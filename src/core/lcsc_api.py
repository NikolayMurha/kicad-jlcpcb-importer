"""Unofficial LCSC API."""

import io
from pathlib import Path
from typing import Optional, Union

import requests  # pylint: disable=import-error


class LCSC_API:
    """Unofficial LCSC API."""

    FILE_DOWNLOAD_BASE = "https://jlcpcb.com/api/file/downloadByFileSystemAccessId/"
    EASYEDA_API_BASE = "https://pro.easyeda.com/api"
    EASYEDA_API_V2_BASE = "https://pro.easyeda.com/api/v2"
    EASYEDA_STEP_BASE = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
        }  # pretend we are browser, otherwise their cloud service blocks the request

    def get_part_data(self, lcsc_number: str) -> dict:
        """Get data for a given LCSC number from the API."""
        r = requests.get(
            f"https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={lcsc_number}",
            headers=self.headers,
            timeout=10,
        )
        if r.status_code != requests.codes.ok:  # pylint: disable=no-member
            return {"success": False, "msg": "non-OK HTTP response status"}
        data = r.json()
        if not data.get("data"):
            return {
                "success": False,
                "msg": "returned JSON data does not have expected 'data' attribute",
            }
        return {"success": True, "data": data}

    def download_bitmap(self, url: str) -> Union[io.BytesIO, None]:
        """Download a picture of the part from the API."""
        content = requests.get(url, headers=self.headers, timeout=10).content
        return io.BytesIO(content)

    def download_datasheet(self, url: str, path: Path):
        """Download and save a datasheet from the API."""
        r = requests.get(url, stream=True, headers=self.headers, timeout=10)
        if r.status_code != requests.codes.ok:  # pylint: disable=no-member
            return {"success": False, "msg": "non-OK HTTP response status"}
        if not r:
            return {"success": False, "msg": "Failed to download datasheet!"}
        with open(path, "wb") as f:
            f.write(r.content)
        return {"success": True, "msg": "Successfully downloaded datasheet!"}

    def easyeda_search_by_codes(self, codes: list[str]) -> dict:
        """Search EasyEDA devices by LCSC codes."""
        r = requests.post(
            f"{self.EASYEDA_API_V2_BASE}/devices/searchByCodes",
            data={"codes[]": codes},
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def easyeda_get_device(self, dev_uuid: str) -> dict:
        """Fetch EasyEDA device details by UUID."""
        r = requests.get(
            f"{self.EASYEDA_API_BASE}/devices/{dev_uuid}",
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def easyeda_get_component(self, component_uuid: str) -> dict:
        """Fetch EasyEDA component details by UUID."""
        r = requests.get(
            f"{self.EASYEDA_API_V2_BASE}/components/{component_uuid}",
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def build_easyeda_step_url(self, model_uuid: str) -> Optional[str]:
        """Build EasyEDA STEP model download URL."""
        if not model_uuid:
            return None
        return f"{self.EASYEDA_STEP_BASE}{model_uuid}"

    def download_easyeda_step_model(self, model_uuid: str, dest: Union[str, Path]) -> str:
        """Download an EasyEDA STEP model by UUID to a local path."""
        url = self.build_easyeda_step_url(model_uuid)
        if not url:
            raise ValueError("Empty model UUID for STEP download")
        self._download_file(url, dest, timeout=60)
        return url

    def _download_file(self, url: str, dest: Union[str, Path], timeout: int = 60) -> None:
        r = requests.get(url, stream=True, headers=self.headers, timeout=timeout)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    def build_file_download_url(self, access_id: str) -> Optional[str]:
        """Build a JLCPCB file download URL from access ID."""
        if not access_id:
            return None
        return f"{self.FILE_DOWNLOAD_BASE}{access_id}"

    def resolve_part_image_url(self, data: dict, size: Optional[str] = None) -> Optional[str]:
        """Resolve the best image URL for part data."""
        picture = data.get("minImage")
        if picture and size:
            picture = picture.replace("96x96", size)
        if picture:
            return picture
        image_id = data.get("productBigImageAccessId")
        return self.build_file_download_url(image_id)
