import time
import webbrowser
from typing import Any

import requests
from pydantic import BaseModel

from . import secure_storage
from .models import Token


class DeviceCodeResponse(BaseModel):
    user_code: str
    device_code: str
    verification_uri: str
    interval: int
    expires_in: int


class AudibleAuthClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.device_code_response: DeviceCodeResponse | None = None

    def get_device_code(self) -> DeviceCodeResponse:
        response = self.session.post(
            "https://api.audible.com/auth/o2/create/codepair",
            json={
                "scope": "alexa:all",
                "scope_data": {
                    "alexa:all": {
                        "productID": "BookBot",
                        "productInstanceAttributes": {"deviceSerialNumber": "12345"},
                    }
                },
            },
        )
        response.raise_for_status()
        self.device_code_response = DeviceCodeResponse.model_validate(response.json())
        return self.device_code_response

    def poll_for_token(self) -> Token:
        if not self.device_code_response:
            raise ValueError("Device code not yet requested.")

        start_time = time.time()
        while time.time() - start_time < self.device_code_response.expires_in:
            response = self.session.post(
                "https://api.audible.com/auth/o2/token",
                json={
                    "grant_type": "device_code",
                    "device_code": self.device_code_response.device_code,
                },
            )

            data = response.json()
            if response.status_code == 200:
                token = Token.model_validate(data)
                secure_storage.save_token(token)
                return token

            error = data.get("error")
            if error == "authorization_pending":
                time.sleep(self.device_code_response.interval)
            elif error:
                raise Exception(f"Failed to get token: {error}")
            else:
                response.raise_for_status()

        raise Exception("Timed out waiting for authorization.")

    def get_license(self, asin: str) -> dict[str, Any]:
        token = secure_storage.load_token()
        if not token:
            raise Exception("Not logged in.")

        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "Content-Type": "application/json",
        }
        response = self.session.post(
            "https://cde-ta-g7g.amazon.com/Firs/v1/license/GetConsumptionLicense",
            headers=headers,
            json={
                "contentId": asin,
                "consumptionType": "Streaming",
                "deviceInfo": {
                    "deviceSerialNumber": "12345",
                    "deviceType": "BookBot",
                },
            },
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_library(self) -> list[dict[str, Any]]:
        """Get user's Audible library."""
        token = secure_storage.load_token()
        if not token:
            raise Exception("Not logged in.")

        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "Content-Type": "application/json",
        }

        response = self.session.get(
            "https://api.audible.com/1.0/library",
            headers=headers,
            params={"num_results": 999, "sort_by": "PurchaseDate"}
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result.get("items", [])

    def download_book(self, asin: str, output_path: str) -> bool:
        """Download an Audible book."""
        license_info = self.get_license(asin)

        # Extract download URL from license
        download_url = license_info.get("content_license", {}).get("content_url")
        if not download_url:
            raise Exception("No download URL found in license")

        # Download the book
        response = self.session.get(download_url, stream=True)
        response.raise_for_status()

        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return True

    def get_activation_bytes(self) -> str | None:
        """Get activation bytes for DRM removal."""
        token = secure_storage.load_token()
        if not token:
            return None

        # This would need to be implemented based on Audible's API
        # For now, return None - users would need to provide their own
        return None

    @staticmethod
    def open_browser(url: str) -> None:
        webbrowser.open(url)
