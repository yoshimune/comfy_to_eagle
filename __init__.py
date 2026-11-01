import hashlib
import http.client
import json
import os
import re
import tempfile
import time

import folder_paths
from comfy_api.latest import ComfyExtension, io, ui


def _request(connection, method, endpoint, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    connection.request(method, endpoint, body=body, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    if response.status != 200:
        raise ValueError(f"Eagle returned HTTP {response.status}.")
    result = json.loads(response.read())
    if not isinstance(result, dict) or result.get("status") != "success":
        raise ValueError(f"Eagle API failed: {endpoint}.")
    return result.get("data")


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _wait_for_copies(items, item_ids, library_path):
    pending = {
        item_id: (os.path.getsize(item["path"]), _file_hash(item["path"]))
        for item, item_id in zip(items, item_ids)
    }
    deadline = time.monotonic() + 60
    while pending:
        for item_id, (size, digest) in list(pending.items()):
            directory = os.path.join(library_path, "images", f"{item_id}.info")
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name.lower().endswith(".png") and entry.is_file(follow_symlinks=False):
                            if entry.stat().st_size == size and _file_hash(entry.path) == digest:
                                del pending[item_id]
                                break
            except (FileNotFoundError, PermissionError):
                # Eagle may still be creating or writing the imported original.
                pass
        if pending:
            if time.monotonic() >= deadline:
                raise TimeoutError("Eagle did not finish copying the images within 60 seconds.")
            time.sleep(0.1)


def _import_items(items):
    connection = http.client.HTTPConnection("127.0.0.1", 41595, timeout=60)
    try:
        library = _request(connection, "GET", "/api/library/info")
        if not isinstance(library, dict) or not isinstance(library.get("library"), dict):
            raise ValueError("Eagle did not return its current library.")
        library_path = library["library"].get("path")
        if not isinstance(library_path, str) or not os.path.isabs(library_path):
            raise ValueError("Eagle did not return an absolute library path.")
        item_ids = _request(connection, "POST", "/api/item/addFromPaths", {"items": items})
        if not isinstance(item_ids, list) or len(item_ids) != len(items):
            raise ValueError("Eagle did not return an import ID for each image; copy completion cannot be verified.")
        if any(not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", item_id) for item_id in item_ids):
            raise ValueError("Eagle returned an invalid import ID.")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("Eagle returned duplicate import IDs.")
    finally:
        connection.close()
    _wait_for_copies(items, item_ids, library_path)


class SendImageToEagle(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="SendImageToEagle",
            display_name="Send Image to Eagle",
            category="image/Eagle",
            description="Send images to the running local Eagle library. Each execution imports again.",
            inputs=[
                io.Image.Input("images"),
                io.String.Input("name", default="ComfyUI"),
                io.String.Input("tags", default="", tooltip="Comma-separated Eagle tags."),
                io.String.Input("annotation", default="", multiline=True),
            ],
            is_output_node=True,
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("NaN")

    @classmethod
    def execute(cls, images, name="ComfyUI", tags="", annotation=""):
        temp_root = os.path.abspath(folder_paths.get_temp_directory())
        os.makedirs(temp_root, exist_ok=True)
        directory = tempfile.mkdtemp(prefix="eagle_", dir=temp_root)
        tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
        items = []
        try:
            for index, image in enumerate(images):
                path = os.path.join(directory, f"image_{index + 1:05}.png")
                with ui.ImageSaveHelper._convert_tensor_to_pil(image) as png:
                    png.save(path, format="PNG", compress_level=4)
                items.append({
                    "path": path,
                    "name": f"{name}_{index + 1:05}" if len(images) > 1 else name,
                    "tags": tag_list,
                    "annotation": annotation,
                })
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(f"Could not prepare Eagle images. Temporary files: {directory}") from error

        try:
            _import_items(items)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise RuntimeError(
                f"Eagle import failed: {error} Open Eagle and check its library. "
                f"Temporary files: {directory}. Import may be partial or complete; "
                "check Eagle before retrying to avoid duplicates."
            ) from error

        try:
            for item in items:
                os.remove(item["path"])
            os.rmdir(directory)
        except OSError as error:
            raise RuntimeError(
                f"Eagle import succeeded, but temporary cleanup failed: {directory}. "
                "Remove the remaining files manually; retrying will import duplicates."
            ) from error
        return io.NodeOutput()


class EagleExtension(ComfyExtension):
    async def get_node_list(self):
        return [SendImageToEagle]


async def comfy_entrypoint():
    return EagleExtension()
