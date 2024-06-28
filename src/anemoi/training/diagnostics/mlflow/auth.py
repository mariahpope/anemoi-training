# (C) Copyright 2024 European Centre for Medium-Range Weather Forecasts.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.


import json
import logging
import os
import time
from getpass import getpass

import requests
from anemoi.utils.config import load_config
from anemoi.utils.config import save_config
from requests.exceptions import HTTPError

LOG = logging.getLogger(__name__)


class TokenAuth:
    """Manage authentication with a keycloak token server."""

    def __init__(
        self,
        url,
        refresh_expire_days=29,
        enabled=True,
    ):
        """
        Parameters
        ----------
        url : str
            URL of the authentication server.
        refresh_expire_days : int, optional
            Number of days before the refresh token expires, by default 29
        enabled : bool, optional
            Set this to False to turn off authentication, by default True
        """

        assert url, "Please provide a URL for the authentication server."

        self.url = url
        self.refresh_expire_days = refresh_expire_days
        self.enabled = enabled

        self.config_file = "mlflow-token.json"
        config = load_config(self.config_file)

        self.refresh_token = config.get("refresh_token")
        self.refresh_expires = config.get("refresh_expires", 0)
        self.access_token = None
        self.access_expires = 0

    def __call__(self):
        self.authenticate()

    def login(self, force_credentials=False, **kwargs):
        """Acquire a new refresh token and save it to disk.

        If an existing valid refresh token is already on disk it will be used.
        If not, or the token has expired, the user will be prompted for credentials.

        This function should be called once, interactively, right before starting a training run.

        Parameters
        ----------
        force_credentials : bool, optional
            Force a username/password prompt even if a refreh token is available, by default False.

        Raises
        ------
        RuntimeError
            A new refresh token could not be acquired.
        """

        if not self.enabled:
            return

        LOG.info(f"Logging in to {self.url}")
        new_refresh_token = None

        if not force_credentials and self.refresh_token and self.refresh_expires > time.time():
            new_refresh_token = self._get_refresh_token(self.refresh_token)

        if not new_refresh_token:
            LOG.info("Please sign in with your credentials.")
            username = input("Username: ")
            password = getpass("Password: ")
            new_refresh_token = self._get_refresh_token(username=username, password=password)

        if new_refresh_token:
            self.refresh_token = new_refresh_token
            self._save_config(new_refresh_token)

            LOG.info("Successfully logged in to MLflow. Happy logging!")
        else:
            raise RuntimeError("Failed to log in. Please try again.")

    def authenticate(self):
        """Check the access token and refresh it if necessary.

        The access token is stored in memory and in the environment variable `MLFLOW_TRACKING_TOKEN`.
        If the access token is still valid, this function does nothing.

        This function should be called before every MLflow API request.

        Raises
        ------
        RuntimeError
            No refresh token is available.
        """

        if not self.enabled:
            return

        if self.access_expires > time.time():
            return

        if not self.refresh_token or self.refresh_expires < time.time():
            raise RuntimeError("You are not logged in to MLflow. Please log in first.")

        self.access_token, self.access_expires = self._get_access_token()

        os.environ["MLFLOW_TRACKING_TOKEN"] = self.access_token
        LOG.debug("Access token refreshed.")

    def _save_config(self, refresh_token):
        refresh_expires = time.time() + (self.refresh_expire_days * 24 * 60 * 60)
        config = {
            "refresh_token": refresh_token,
            "refresh_expires": int(refresh_expires),
        }
        save_config(self.config_file, config)

    def _get_refresh_token(self, refresh_token=None, username=None, password=None):
        if refresh_token:
            path = "refreshtoken"
            payload = {"refresh_token": refresh_token}
        else:
            path = "newtoken"
            payload = {"username": username, "password": password}

        response = self._request(path, payload)

        return response.get("refresh_token")

    def _get_access_token(self):
        payload = {"refresh_token": self.refresh_token}
        response = self._request("refreshtoken", payload)

        token = response.get("access_token")
        expires_in = response.get("expires_in")

        expires = time.time() + (expires_in * 0.7)  # some buffer time

        return token, expires

    def _request(self, path, payload):

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            response = requests.post(f"{self.url}/{path}", headers=headers, json=payload)
            response.raise_for_status()
            response_json = response.json()

            if response_json.get("status", "") == "ERROR":
                # TODO: there's a bug in the API that returns the error response as a string instead of a json object.
                # Remove this when the API is fixed.
                if isinstance(response_json["response"], str):
                    error = json.loads(response_json["response"])
                else:
                    error = response_json["response"]
                LOG.warning(error.get("error_description", "Error acquiring token."))
                # don't raise here, let the caller decide what to do if no token is acquired
                return {}

            return response_json["response"]
        except HTTPError as http_err:
            LOG.error(f"HTTP error occurred: {http_err}")
            raise
        except Exception as err:
            LOG.error(f"Other error occurred: {err}")
            raise
