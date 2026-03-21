import copy
import os
import json
import traceback
import concurrent.futures
import zipfile

from logging import info, warning, debug, error, critical
from typing import Callable, Optional

from pcbnew import *  # type: ignore

from . import decryptor
from ...core.lcsc_api import LCSC_API

_REQUESTS_IMPORT_ERROR = None
_REQUESTS = None

def _load_requests():
    global _REQUESTS, _REQUESTS_IMPORT_ERROR
    if _REQUESTS is not None:
        return _REQUESTS
    try:
        import requests as _requests
        _REQUESTS = _requests
        _REQUESTS_IMPORT_ERROR = None
        return _REQUESTS
    except Exception as exc:
        _REQUESTS_IMPORT_ERROR = exc
        return None

def _require_requests():
    req = _load_requests()
    if req is None:
        msg = "Please install requests using: pip install requests --target ./lib"
        raise Exception(msg) from _REQUESTS_IMPORT_ERROR
    return req

MODELS_DIR = "3dmodels"

# UUID strings can be in the format <uuid>|<owner_uuid>. This function gets the <uuid> part
def getUuidFirstPart(uuid):
    if not uuid:
        return None
    return uuid.split("|")[0]


# Extract dataStr from component data. If dataStr is not available, try to decrypt and decompress the data from dataStrId URL.
def extractDataStr(component_data):
    if not component_data:
        return None

    req = _require_requests()

    # Try direct dataStr first
    dataStr = component_data.get("dataStr")
    if dataStr:
        return dataStr
        
    # Try dataStrId if dataStr not available
    dataStrId = component_data.get("dataStrId")
    if dataStrId:
        try:
            keyHex = component_data.get("key")
            ivHex = component_data.get("iv")

            debug("dataStrId key: " + keyHex)
            debug("dataStrId iv: " + ivHex)
            
            dataStrResp = req.get(dataStrId)
            dataStrResp.raise_for_status()

            debug("dataStrId encrypted content: " + dataStrResp.content.hex())

            decryptedStr = decryptor.decryptDataStrIdData(dataStrResp.content, keyHex, ivHex)

            debug("dataStrId decrypted content: " + decryptedStr)

            return decryptedStr
        except Exception as e:
            info(f"Failed to fetch/decrypt dataStrId: {e}")
            
    return None

class ComponentLoader():
    def __init__(
        self,
        kiprjmod,
        target_path,
        target_name,
        progress: Callable[[int, int], None],
        models_dir: Optional[str] = None,
    ):
        self.kiprjmod = kiprjmod
        self.target_path = target_path
        self.target_name = target_name
        self.progress = progress
        self.lcsc_api = LCSC_API()
        if models_dir:
            self.models_dir = models_dir
        else:
            self.models_dir = os.path.join(self.kiprjmod, MODELS_DIR)

    def downloadAll(self, components):
        self.progress(0, 100)

        try:
            libDeviceFile, fetched_3dmodels = self.downloadSymFp(components)
            self.downloadModels(libDeviceFile, fetched_3dmodels)
            self.progress(100, 100)
        except Exception as e:
            traceback.print_exc()
            error(f"Failed to download components: {traceback.format_exc()}")

    def downloadSymFp(self, components):
        info(f"Fetching info...")

        # Separate components into code-based and direct UUIDs
        code_components = []
        direct_uuids = []

        for comp in components:
            if comp.startswith("C"):
                code_components.append(comp)
            else:
                direct_uuids.append(comp)

        fetched_devices = {}

        # Fetch UUIDs from code-based components
        if code_components:
            found = self.lcsc_api.easyeda_search_by_codes(code_components)

            debug("searchByCodes: " + json.dumps(found, indent=4))

            if not found.get("success") or not found.get("result"):
                raise Exception(f"Unable to fetch device info: {found}")

            # Append fetched UUIDs to direct_uuids
            for entry in found["result"]:
                direct_uuids.append(entry['uuid'])

        # Fetch device info by UUID
        def fetch_device_info(dev_uuid):
            dev_info = self.lcsc_api.easyeda_get_device(dev_uuid)
            debug("device info: " + json.dumps(dev_info, indent=4))
            result = dev_info.get("result")
            if not result:
                raise Exception(f"Empty result for device UUID {dev_uuid}: {dev_info}")
            fetched_devices[result["uuid"]] = result

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(fetch_device_info, dev_uuid) for dev_uuid in direct_uuids]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    error(f"Failed to fetch device info: {e}")

        # Collect symbol/footprint/3D model UUIDs to fetch
        fetched_symbols = {}
        fetched_footprints = {}
        fetched_3dmodels = {}
        uuid_to_obj_map = {}

        all_uuids = set()
        for entry in fetched_devices.values():
            if entry['attributes'].get('Symbol'):
                all_uuids.add(entry['attributes']['Symbol'])
                uuid_to_obj_map[entry['attributes']['Symbol']] = fetched_symbols

            if entry['attributes'].get('Footprint'):
                all_uuids.add(entry['attributes']['Footprint'])
                uuid_to_obj_map[entry['attributes']['Footprint']] = fetched_footprints

            if entry['attributes'].get('3D Model'):
                all_uuids.add(getUuidFirstPart(entry['attributes']['3D Model']))
                uuid_to_obj_map[getUuidFirstPart(entry['attributes']['3D Model'])] = fetched_3dmodels

        # Fetch symbols/footprints/3D models
        def fetch_component(uuid):
            comp_info = self.lcsc_api.easyeda_get_component(uuid)
            return comp_info["result"]

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = {executor.submit(fetch_component, uuid): uuid for uuid in all_uuids}
            for future in concurrent.futures.as_completed(futures):
                try:
                    compData = future.result()
                    debug(f"Fetched component {json.dumps(compData, indent=4)}")

                    uuid_to_obj_map[compData["uuid"]][compData["uuid"]] = compData
                except Exception as e:
                    error(f"Failed to fetch component for uuid {futures[future]}: {e}")

        # Set symbol/footprint type fields
        for device in fetched_devices.values():
            sym_uuid = device['attributes'].get('Symbol')
            if sym_uuid and sym_uuid in fetched_symbols:
                fetched_symbols[sym_uuid]["type"] = device.get("symbol_type", "")

            fp_uuid = device['attributes'].get('Footprint')
            if fp_uuid and fp_uuid in fetched_footprints:
                fetched_footprints[fp_uuid]["type"] = device.get("footprint_type", "")

        # Extract dataStr
        footprint_data_str = {}
        symbol_data_str = {}

        # Separate dataStr for footprints
        for f_uuid, f_data in fetched_footprints.items():
            ds = extractDataStr(f_data)
            if ds:
                footprint_data_str[f_uuid] = ds

            f_data.pop("dataStr", None) # Remove the dataStr field if exists

        # Build sym_uuid -> device name mapping for name patching
        sym_uuid_to_name = {}
        for device in fetched_devices.values():
            sym_uuid = device.get("attributes", {}).get("Symbol")
            if not sym_uuid:
                continue
            name = (
                device.get("display_title")
                or device.get("title")
                or device.get("name")
                or device.get("product_code")
                or ""
            ).strip()
            if name:
                sym_uuid_to_name[sym_uuid] = name

        # Separate dataStr for symbols
        for s_uuid, s_data in fetched_symbols.items():
            ds = extractDataStr(s_data)
            if ds:
                # Patch head.name if it is empty or "~"
                dev_name = sym_uuid_to_name.get(s_uuid, "")
                if dev_name:
                    try:
                        sym_obj = json.loads(ds)
                        head = sym_obj.get("head") if isinstance(sym_obj, dict) else None
                        if isinstance(head, dict) and head.get("name", "").strip() in ("", "~"):
                            head["name"] = dev_name
                            ds = json.dumps(sym_obj, ensure_ascii=False)
                    except Exception:
                        pass
                symbol_data_str[s_uuid] = ds

            s_data.pop("dataStr", None) # Remove the dataStr field if exists

        libDeviceFile = {
            "devices": fetched_devices,
            "symbols": fetched_symbols,
            "footprints": fetched_footprints
        }

        os.makedirs(self.target_path, exist_ok=True)

        zip_filename = os.path.join(self.target_path, f"{self.target_name}.elibz")
        merged_data = copy.deepcopy(libDeviceFile)

        try:
            if os.path.exists(zip_filename):
                with zipfile.ZipFile(zip_filename, "r") as old_zip:
                    for name in old_zip.namelist():
                        if name == "device.json":
                            old_data = json.loads(old_zip.read("device.json").decode("utf-8"))
                            for entry_type in ["devices", "symbols", "footprints"]:
                                for key in old_data[entry_type]:
                                    if key not in merged_data[entry_type]:
                                        merged_data[entry_type][key] = old_data[entry_type][key]
                        if name.endswith('.esym'):
                            symbol_uuid = os.path.splitext(os.path.basename(name))[0]
                            if symbol_uuid not in symbol_data_str:
                                symbol_data_str[symbol_uuid] = old_zip.read(name).decode('utf-8')
                        elif name.endswith('.efoo'):
                            footprint_uuid = os.path.splitext(os.path.basename(name))[0]
                            if footprint_uuid not in footprint_data_str:
                                footprint_data_str[footprint_uuid] = old_zip.read(name).decode('utf-8')
        except Exception as e:
            warning(f"Failed to merge device.json data, overwriting: {e}")

        with zipfile.ZipFile(zip_filename, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("device.json", json.dumps(merged_data, indent=4))
            for fp_uuid, ds in footprint_data_str.items():
                zf.writestr(f"FOOTPRINT/{fp_uuid}.efoo", ds)
            for sym_uuid, ds in symbol_data_str.items():
                zf.writestr(f"SYMBOL/{sym_uuid}.esym", ds)

        info( "*****************************" )
        info(f"Downloaded {len(fetched_devices)} devices, {len(fetched_symbols)} symbols, {len(fetched_footprints)} footprints and added to library: {zip_filename}")
        return libDeviceFile, fetched_3dmodels

    def downloadModels(self, libDeviceFile, fetched_3dmodels):
        self.totalToDownload = 0
        self.downloadedCounter = 0
        self.statExisting = 0
        self.statDownloaded = 0
        self.statFailed = 0

        info( "*****************************" )
        info(f"Loading 3D models...")
        self.progress(0, 100)

        uuidToTargetFileMap = {}
        uuidsToTransform = {}

        debug("fetched_3dmodels: " + json.dumps(fetched_3dmodels, indent=4))
        debug("libDeviceFile: " + json.dumps(libDeviceFile, indent=4))

        for device in libDeviceFile["devices"].values():
            try:
                modelUuid = getUuidFirstPart(device["attributes"].get("3D Model"))

                if not modelUuid or modelUuid not in fetched_3dmodels:
                    info("No model for device '%s', footprint '%s'"
                         % (device.get("product_code", device.get("uuid")), 
                            device.get("footprint").get("display_title") if device.get("footprint") else "None"))
                    continue

                modelTitle = device["attributes"]["3D Model Title"]
                modelTransform = device["attributes"].get("3D Model Transform", "")

                dataStr = extractDataStr(fetched_3dmodels[modelUuid])

                if dataStr:
                    directUuid = json.loads(dataStr)["model"]
                else:
                    info("Unable to extract model for device '%s', footprint '%s'"
                         % (device.get("product_code", device.get("uuid")), 
                            device.get("footprint").get("display_title") if device.get("footprint") else "None"))
                    continue

                parts = [x.strip() for x in modelTransform.split(",") if x.strip()]
                if len(parts) < 2:
                    info(f"Skipping 3D transform for '{modelTitle}': not enough values in '{modelTransform}'")
                    continue
                uuidsToTransform[directUuid] = [float(x) for x in parts]

                os.makedirs(self.models_dir, exist_ok=True)
                easyEdaFilename = os.path.join(self.models_dir, modelTitle + ".step")
                easyEdaFilename = os.path.normpath(easyEdaFilename)

                uuidToTargetFileMap[directUuid] = easyEdaFilename
            except KeyboardInterrupt:
                return
            except Exception as e:
                traceback.print_exc()
                info("Cannot get model for device '%s': %s" % (device.get("product_code", device.get("uuid")), str(e)))
                continue

        with concurrent.futures.ThreadPoolExecutor(1) as texecutor:
            def fixupModel(fixTaskArgs):
                directUuid, kfilePath = fixTaskArgs

                file_name = os.path.splitext( os.path.basename( kfilePath ) ) [0]
                jfilePath = kfilePath + "_jlc"

                debug( "Loading STEP model %s" % (file_name) )
                model: UTILS_STEP_MODEL = UTILS_STEP_MODEL.LoadSTEP(jfilePath)

                if not model:
                    error( "Error loading model '%s'" % (file_name) )
                    return
                
                debug( "Converting STEP model '%s'" % (file_name) )
                bbox: UTILS_BOX3D = model.GetBoundingBox()

                try:
                    if directUuid in uuidsToTransform:
                        # Convert mils to mm
                        fitXmm = uuidsToTransform[directUuid][0] / 39.37
                        fitYmm = uuidsToTransform[directUuid][1] / 39.37

                        bsize: VECTOR3D = bbox.GetSize()
                        scaleFactorX = fitXmm / bsize.x;
                        scaleFactorY = fitYmm / bsize.y;
                        scaleFactor = ( scaleFactorX + scaleFactorY ) / 2

                        debug( "Dimensions %f %f factors %f %f avg %f model '%s'" %
                            (fitXmm, fitYmm, scaleFactorX, scaleFactorY, scaleFactor, file_name) )

                        if abs( scaleFactorX - scaleFactorY ) > 0.1:
                            warning( "Scale factors do not match: X %.3f; Y %.3f for model '%s'." %
                                (scaleFactorX, scaleFactorY, file_name) )
                            warning( "**** The model '%s' might be misoriented! ****" % (file_name) )
                        elif abs( scaleFactor - 1.0 ) > 0.01:
                            warning( "Scaling '%s' by %f" % (file_name, scaleFactor) )
                            model.Scale( scaleFactor );
                        else:
                            debug( "No scaling for %s" % (file_name) )

                except Exception as e:
                    traceback.print_exc()
                    error( "Error scaling model '%s': %s" % (file_name, str(e)) )
                    return

                newbbox          = model.GetBoundingBox()
                center: VECTOR3D = newbbox.GetCenter()

                model.Translate( -center.x, -center.y, -newbbox.Min().z )

                debug( "Saving STEP model %s" % (file_name) )
                model.SaveSTEP( kfilePath )

                # Delete the temporary JLC file after successful conversion
                try:
                    if os.path.exists(jfilePath):
                        os.remove(jfilePath)
                        debug(f"Deleted temporary file {jfilePath}")
                except Exception as e:
                    info(f"Failed to delete temporary file {jfilePath}: {str(e)}")

            with concurrent.futures.ThreadPoolExecutor(8) as dexecutor: 
                def downloadStep(dnlTaskArgs):
                    directUuid, kfilePath = dnlTaskArgs
                    file_name = os.path.splitext( os.path.basename( kfilePath ) ) [0]

                    try:
                        if not os.path.exists(kfilePath):
                            jfilePath = kfilePath + "_jlc"
                            url = self.lcsc_api.build_easyeda_step_url(directUuid)
                            if not url:
                                raise ValueError("Empty EasyEDA STEP model URL")

                            debug("Downloading '%s'" % (file_name))
                            debug("'%s' from '%s'" % (file_name, url))
                            os.makedirs(os.path.dirname(kfilePath), exist_ok=True)
                            self.lcsc_api.download_easyeda_step_model(directUuid, jfilePath)

                            if os.path.isfile(jfilePath):
                                debug("Downloaded '%s'." % (file_name))
                                self.statDownloaded += 1

                                fixTaskArgs = [directUuid, kfilePath]
                                texecutor.submit(fixupModel, fixTaskArgs)
                            else:
                                warning( "Path '%s' is not a file." % jfilePath )
                        else:
                            info("Skipping '%s': STEP model file already exists." % (file_name))
                            self.statExisting += 1

                    except Exception as e:
                        warning("Failed to download model '%s': %s" % (file_name, str(e)))
                        self.statFailed += 1

                    self.downloadedCounter += 1
                    self.progress(self.downloadedCounter, self.totalToDownload)

                self.totalToDownload = len(uuidToTargetFileMap)
                dexecutor.map(downloadStep, uuidToTargetFileMap.items())

        info( "" )
        info( "*****************************" )
        info( "          All done.          " )
        info( "*****************************" )
        info( "" )
        info( "Total model count: %d" % len(uuidToTargetFileMap) )
        info( "STEP models downloaded: %d" % self.statDownloaded )
        info( "Already existing models: %d" % self.statExisting )
        info( "Failed downloads: %d" % self.statFailed )
        self.progress(100, 100)
