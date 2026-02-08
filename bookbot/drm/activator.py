"""Audible activation code retrieval using selenium."""

import base64
import binascii
import hashlib
import sys

import requests
from selenium import webdriver

PY3 = sys.version_info[0] == 3

if PY3:
    from urllib.parse import parse_qsl, urlparse
else:
    from urlparse import parse_qsl, urlparse


def extract_activation_bytes(data: bytes) -> tuple[str, list[str]]:
    """Extracts activation bytes from the activation blob."""
    if (b"BAD_LOGIN" in data or b"Whoops" in data) or b"group_id" not in data:
        raise Exception("Activation failed. Please check your credentials.")

    k = data.rfind(b"group_id")
    l = data[k:].find(b")")
    keys = data[k + l + 1 + 1:]
    output = []
    output_keys = []
    # each key is of 70 bytes
    for i in range(0, 8):
        key = keys[i * 70 + i:(i + 1) * 70 + i]
        h = binascii.hexlify(bytes(key))
        h = b",".join(h[i:i + 2] for i in range(0, len(h), 2))
        output_keys.append(h)
        output.append(h.decode('utf-8'))

    # only 4 bytes of output_keys[0] are necessary for decryption! ;)
    activation_bytes = output_keys[0].replace(b",", b"")[0:8]
    # get the endianness right (reverse string in pairs of 2)
    activation_bytes = b"".join(reversed([activation_bytes[i:i + 2] for i in
                                         range(0, len(activation_bytes), 2)]))
    if PY3:
        activation_bytes = activation_bytes.decode("ascii")

    return activation_bytes, output


def fetch_activation_bytes(driver: webdriver.Chrome, lang: str = "us") -> str:
    """Fetches activation bytes from Audible using an authenticated selenium webdriver."""
    base_url = 'https://www.audible.com/'
    base_url_license = 'https://www.audible.com/'

    if lang == "uk":
        base_url = base_url.replace('.com', ".co.uk")
    elif lang == "jp":
        base_url = base_url.replace('.com', ".co.jp")
    elif lang == "au":
        base_url = base_url.replace('.com', ".com.au")
    elif lang == "in":
        base_url = base_url.replace('.com', ".in")
    elif lang != "us":
        base_url = base_url.replace('.com', "." + lang)

    if PY3:
        player_id = base64.encodebytes(hashlib.sha1(b"").digest()).rstrip()
        player_id = player_id.decode("ascii")
    else:
        player_id = base64.encodestring(hashlib.sha1(b"").digest()).rstrip()

    # Step 2
    driver.get(base_url + 'player-auth-token?playerType=software&bp_ua=y&playerModel=Desktop&playerId=%s&playerManufacturer=Audible&serial=' % (player_id))
    current_url = driver.current_url
    o = urlparse(current_url)
    data = dict(parse_qsl(o.query))

    # Step 2.5, switch User-Agent to "Audible Download Manager"
    headers = {
        'User-Agent': "Audible Download Manager",
    }
    cookies = driver.get_cookies()
    s = requests.Session()
    for cookie in cookies:
        s.cookies.set(cookie['name'], cookie['value'])

    # Step 3, de-register first, in order to stop hogging all activation slots
    durl = base_url_license + 'license/licenseForCustomerToken?' + 'customer_token=' + data["playerToken"] + "&action=de-register"
    s.get(durl, headers=headers)

    # Step 4
    url = base_url_license + 'license/licenseForCustomerToken?' + 'customer_token=' + data["playerToken"]
    response = s.get(url, headers=headers)

    activation_bytes, _ = extract_activation_bytes(response.content)

    # Step 5 (de-register again to stop filling activation slots)
    s.get(durl, headers=headers)

    return activation_bytes
