"""Unofficial LCSC API."""

import io
import json
import re
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote_plus

try:
    import requests  # pylint: disable=import-error
except ImportError:
    requests = None


def _require_requests():
    """Load requests after the runtime dependency installer has run."""

    global requests
    if requests is None:
        import requests as requests_module  # pylint: disable=import-error,import-outside-toplevel

        requests = requests_module
    return requests


class LCSC_API:
    """Unofficial LCSC API."""

    FILE_DOWNLOAD_BASE = "https://jlcpcb.com/api/file/downloadByFileSystemAccessId/"
    EASYEDA_API_BASE = "https://pro.easyeda.com/api"
    EASYEDA_API_V2_BASE = "https://pro.easyeda.com/api/v2"
    EASYEDA_STEP_BASE = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/"
    LCSC_SEARCH_BASE = "https://www.lcsc.com/search"
    LCSC_PRODUCT_BASE = "https://www.lcsc.com/product-detail"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
        }  # pretend we are browser, otherwise their cloud service blocks the request

    def get_part_data(self, lcsc_number: str) -> dict:
        """Get data for a given LCSC number from the API."""
        request = _require_requests()
        r = request.get(
            f"https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={lcsc_number}",
            headers=self.headers,
            timeout=10,
        )
        if r.status_code != request.codes.ok:  # pylint: disable=no-member
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
        content = _require_requests().get(url, headers=self.headers, timeout=10).content
        return io.BytesIO(content)

    def download_datasheet(self, url: str, path: Path):
        """Download and save a datasheet from the API."""
        request = _require_requests()
        r = request.get(url, stream=True, headers=self.headers, timeout=10)
        if r.status_code != request.codes.ok:  # pylint: disable=no-member
            return {"success": False, "msg": "non-OK HTTP response status"}
        if not r:
            return {"success": False, "msg": "Failed to download datasheet!"}
        with open(path, "wb") as f:
            f.write(r.content)
        return {"success": True, "msg": "Successfully downloaded datasheet!"}

    def easyeda_search_by_codes(self, codes: list[str]) -> dict:
        """Search EasyEDA devices by LCSC codes."""
        r = _require_requests().post(
            f"{self.EASYEDA_API_V2_BASE}/devices/searchByCodes",
            data={"codes[]": codes},
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def easyeda_get_device(self, dev_uuid: str) -> dict:
        """Fetch EasyEDA device details by UUID."""
        r = _require_requests().get(
            f"{self.EASYEDA_API_BASE}/devices/{dev_uuid}",
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def easyeda_get_component(self, component_uuid: str) -> dict:
        """Fetch EasyEDA component details by UUID."""
        r = _require_requests().get(
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
        r = _require_requests().get(url, stream=True, headers=self.headers, timeout=timeout)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    def get_3d_model_direct_uuid(self, model_component_uuid: str) -> Optional[str]:
        """Resolve the direct STEP model file UUID from a 3D model component UUID.

        EasyEDA stores 3D models in two levels:
          1. The device attribute "3D Model" holds a *component* UUID (may have "|owner" suffix).
          2. Fetching that component via easyeda_get_component() returns a dataStr JSON whose
             "model" key contains the actual STEP file UUID used in the download URL.
        """
        raw = str(model_component_uuid or "").strip()
        model_uuid = raw.split("|")[0].strip()
        if not model_uuid:
            return None
        try:
            comp_info = self.easyeda_get_component(model_uuid)
            comp_data = comp_info.get("result") or {}
            data_str = comp_data.get("dataStr")
            if not data_str:
                return None
            direct_uuid = str(json.loads(data_str).get("model") or "").strip()
            return direct_uuid or None
        except Exception:
            return None

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

    @staticmethod
    def normalize_external_url(url: Optional[str], default_host: str = "https://www.lcsc.com") -> str:
        """Normalize URL-like text to an absolute HTTPS URL where possible."""
        text = str(url or "").strip()
        if not text:
            return ""
        text = text.replace("\\", "/")
        text = re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*)//", r"\1://", text)
        if text.startswith("//"):
            return "https:" + text
        if text.startswith("/"):
            return default_host.rstrip("/") + text
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
            return text
        if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}([/:]|$)", text):
            return "https://" + text
        return text

    def extract_lcsc_product_url(self, url: Optional[str]) -> str:
        """Extract canonical LCSC product URL from malformed URL-like text."""
        text = str(url or "").strip().replace("\\", "/")
        if not text:
            return ""
        lower = text.lower()
        if "lcsc" not in lower and "product-detail" not in lower:
            return ""

        # Handles full and malformed forms like:
        # https://lcsc.comhttps//lcsc.com/product-detail/..._C123456.html/?href=...
        match = re.search(
            r"(?:https?:?//)?(?:www\.)?lcsc\.com/(product-detail/[^?#\s]*?\.html)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            match = re.search(
                r"(product-detail/[^?#\s]*?\.html)",
                text,
                flags=re.IGNORECASE,
            )
        if match:
            path = match.group(1).lstrip("/").rstrip("/")
            return f"{self.LCSC_SEARCH_BASE.rsplit('/', 1)[0]}/{path}"

        code_match = re.search(r"\b(C\d{4,})\b", text, flags=re.IGNORECASE)
        if code_match:
            return f"{self.LCSC_PRODUCT_BASE}/{code_match.group(1).upper()}.html"
        return ""

    def build_lcsc_search_url(self, lcsc_number: str) -> str:
        code = str(lcsc_number or "").strip()
        if not code:
            return self.LCSC_SEARCH_BASE
        if re.fullmatch(r"C\d{4,}", code, flags=re.IGNORECASE):
            return f"{self.LCSC_PRODUCT_BASE}/{code.upper()}.html"
        return f"{self.LCSC_SEARCH_BASE}?q={quote_plus(code)}"

    def resolve_part_page_url(self, data: Optional[dict], lcsc_number: Optional[str] = None) -> str:
        """Resolve best available LCSC page URL with robust fallback by code."""
        info = data or {}
        code = str(lcsc_number or info.get("componentCode") or "").strip()
        if re.fullmatch(r"C\d{4,}", code, flags=re.IGNORECASE):
            return f"{self.LCSC_PRODUCT_BASE}/{code.upper()}.html"

        for key in (
            "lcscGoodsUrl",
            "goodsUrl",
            "productUrl",
            "productDetailUrl",
            "componentUrl",
            "url",
        ):
            raw = info.get(key)
            canonical = self.extract_lcsc_product_url(raw)
            if canonical:
                return canonical
            candidate = self.normalize_external_url(raw)
            canonical = self.extract_lcsc_product_url(candidate)
            if canonical:
                return canonical
            if candidate:
                return candidate

        if code:
            return self.build_lcsc_search_url(code)
        return self.LCSC_SEARCH_BASE

    def resolve_datasheet_url(self, data: Optional[dict]) -> str:
        """Resolve datasheet URL and normalize it."""
        info = data or {}
        for key in ("dataManualUrl", "manualUrl", "datasheetUrl", "dataSheetUrl"):
            candidate = self.normalize_external_url(info.get(key))
            if candidate:
                return candidate
        return ""
