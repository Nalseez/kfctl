import argparse
import base64
import datetime
import logging
import os
import errno
import shutil
import subprocess
import tempfile
import threading
from functools import partial
from multiprocessing import Process
from time import sleep
from google.auth.transport.requests import Request
from googleapiclient import discovery
from oauth2client.client import GoogleCredentials

import requests
import yaml
import google.auth
import google.auth.compute_engine.credentials
import google.auth.iam
import google.oauth2.credentials
import google.oauth2.service_account
from retrying import retry
from requests.exceptions import SSLError
from requests.exceptions import ConnectionError as ReqConnectionError

IAM_SCOPE = "https://www.googleapis.com/auth/iam"
OAUTH_TOKEN_URI = "https://www.googleapis.com/oauth2/v4/token"
COOKIE_NAME = "KUBEFLOW-AUTH-KEY"

def get_service_account_credentials(client_id_key):
  # Figure out what environment we're running in and get some preliminary
  # information about the service account.
  credentials, _ = google.auth.default(scopes=[IAM_SCOPE])
  if isinstance(credentials, google.oauth2.credentials.Credentials):
    raise Exception("make_iap_request is only supported for service "
                    "accounts.")

  # For service account's using the Compute Engine metadata service,
  # service_account_email isn't available until refresh is called.
  credentials.refresh(Request())

  signer_email = credentials.service_account_email
  if isinstance(credentials,
                google.auth.compute_engine.credentials.Credentials):
    signer = google.auth.iam.Signer(Request(), credentials, signer_email)
  else:
    # A Signer object can sign a JWT using the service account's key.
    signer = credentials.signer

  # Construct OAuth 2.0 service account credentials using the signer
  # and email acquired from the bootstrap credentials.
  return google.oauth2.service_account.Credentials(
      signer,
      signer_email,
      token_uri=OAUTH_TOKEN_URI,
      additional_claims={"target_audience": may_get_env_var(client_id_key)})

def get_google_open_id_connect_token(service_account_credentials):
  service_account_jwt = (
      service_account_credentials._make_authorization_grant_assertion())
  request = google.auth.transport.requests.Request()
  body = {
      "assertion": service_account_jwt,
      "grant_type": google.oauth2._client._JWT_GRANT_TYPE,
  }
  token_response = google.oauth2._client._token_endpoint_request(
      request, OAUTH_TOKEN_URI, body)
  return token_response["id_token"]

def may_get_env_var(name):
  env_val = os.getenv(name)
  if env_val:
    logging.info("%s is set" % name)
    return env_val
  else:
    raise Exception("%s not set" % name)

def iap_is_ready(url, wait_min=15):
  """
  Checks if the kubeflow endpoint is ready.

  Args:
    url: The url endpoint
  Returns:
    True if the url is ready
  """
  google_open_id_connect_token = None

  service_account_credentials = get_service_account_credentials("CLIENT_ID")
  google_open_id_connect_token = get_google_open_id_connect_token(
      service_account_credentials)
  # Wait up to 30 minutes for IAP access test.
  num_req = 0
  end_time = datetime.datetime.now() + datetime.timedelta(
      minutes=wait_min)
  while datetime.datetime.now() < end_time:
    num_req += 1
    logging.info("Trying url: %s", url)
    try:
      resp = None
      resp = requests.request(
          "GET",
          url,
          headers={
              "Authorization":
              "Bearer {}".format(google_open_id_connect_token)
          },
          verify=False)
      logging.info(resp.text)
      if resp.status_code == 200:
        logging.info("Endpoint is ready for %s!", url)
        return True
      else:
        logging.info(
            "%s: Endpoint not ready, request number: %s" % (url, num_req))
    except Exception as e:
      logging.info("%s: Endpoint not ready, exception caught %s, request number: %s" %
                   (url, str(e), num_req))
    sleep(10)
  return False

def _send_req(wait_sec, url, req_gen, retry_result_code=None):
  """ Helper function to send requests and retry when the endpoint is not ready.

  Args:
  wait_sec: int, max time to wait and retry in seconds.
  url: str, url to send the request, used only for logging.
  req_gen: lambda, no parameter function to generate requests.Request for the
  function to send to the endpoint.
  retry_result_code: int (optional), status code to match or retry the request.

  Returns:
    requests.Response
  """

  def retry_on_error(e):
    return isinstance(e, (SSLError, ReqConnectionError))

  # generates function to see if the request needs to be retried.
  # if param `code` is None, will not retry and directly pass back the response.
  # Otherwise will retry if status code is not matched.
  def retry_on_result_func(code):
    if code is None:
      return lambda _: False
    else:
      return lambda resp: not resp or resp.status_code != code

  @retry(stop_max_delay=wait_sec * 1000, wait_fixed=10 * 1000,
         retry_on_exception=retry_on_error,
         retry_on_result=retry_on_result_func(retry_result_code))
  def _send(url, req_gen):
    resp = None
    logging.info("sending request to %s" % url)
    try:
      resp = req_gen()
    except Exception as e:
      logging.warning("%s: request with error: %s" % (url, e))
      raise e
    return resp

  return _send(url, req_gen)

def basic_auth_is_ready(url, username, password, wait_min=15):
  get_url = url + "/kflogin"
  post_url = url + "/apikflogin"

  end_time = datetime.datetime.now() + datetime.timedelta(
      minutes=wait_min)

  wait_time = datetime.datetime.now() - end_time
  resp = _send_req(wait_time.seconds, get_url, lambda: requests.request(
      "GET",
      get_url,
      verify=False), retry_result_code=200)

  logging.info("%s: endpoint is ready; response: %s" % (get_url, resp.text))
  logging.info("%s: testing login API" % post_url)

  wait_time = datetime.datetime.now() - end_time
  resp = _send_req(wait_time.seconds, post_url, lambda: requests.post(
      post_url,
      auth=(username, password),
      headers={
          "x-from-login": "true",
      },
      verify=False))
  logging.info("%s: %s" % (post_url, resp.text))
  if resp.status_code != 205:
    logging.error("%s: login is failed", post_url)
    return False

  logging.info("%s: testing cookies credentials" % url)
  cookie = None
  for c in resp.cookies:
    if c.name == COOKIE_NAME:
      cookie = c
      break
  if cookie is None:
    logging.error("%s: auth cookie cannot be found; name: %s" % (post_url, COOKIE_NAME))
    return False

  wait_time = datetime.datetime.now() - end_time
  resp = _send_req(wait_time.seconds, url, lambda: requests.get(
      url,
      cookies={
          cookie.name: cookie.value,
      },
      verify=False))
  logging.info("%s: %s" % (url, resp.status_code))
  logging.info(resp.content)
  return resp.status_code == 200
