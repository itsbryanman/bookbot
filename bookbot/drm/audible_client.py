import time
import webbrowser
from typing import Any, Dict, Optional

import requests
from pydantic import BaseModel, Field

from bookbot.core.models import Token
from bookbot.drm import secure_storage


class DeviceCodeResponse(BaseModel):
    user_code: str
    device_code: str
    verification_uri: str
    interval: int
    expires_in: int


class AudibleAuthClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.device_code_response: Optional[DeviceCodeResponse] = None

    def get_device_code(self) -> DeviceCodeResponse:
        response = self.session.post(
            "https://api.audible.com/auth/o2/create/codepair",
            json={
                "scope": "alexa:all",
                "scope_data": {
                    "alexa:all": {
                        "productID": "Platescape",
                        "productInstanceAttributes": {"deviceSerialNumber": "12345"},
                    }
                },
            },
        )
        response.raise_for_status()
        self.device_code_response = DeviceCodeResponse.parse_obj(response.json())
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
                token = Token.parse_obj(data)
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

    def get_license(self, asin: str) -> Dict[str, Any]:
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
                    "deviceType": "Platescape",
                },
            },
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def open_browser(url: str) -> None:
        webbrowser.open(url)