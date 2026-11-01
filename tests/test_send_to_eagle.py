import asyncio
import copy
import http.client
import json
import shutil
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import torch
from PIL import Image

import nodes
from comfy_api.latest import ComfyExtension, io
from custom_nodes import comfyui_to_eagle as eagle
from custom_nodes.comfyui_png_provenance import SaveImageWithProvenance
from custom_nodes.comfyui_png_provenance.png_bundle import read_png_bundle
from custom_nodes.comfyui_png_provenance.restore import restore_png
from execution import PromptExecutor


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    monkeypatch.setattr(eagle.folder_paths, "get_temp_directory", lambda: str(tmp_path / "temp"))
    connection = MagicMock()
    connection.getresponse.return_value.status = 200

    def respond():
        call = connection.request.call_args
        if call.args[0] == "GET":
            return json.dumps({"status": "success", "data": {"library": {"path": str(tmp_path / "library")}}}).encode()
        items = json.loads(call.kwargs["body"])["items"]
        item_ids = []
        for index, item in enumerate(items):
            item_id = f"item{index}"
            directory = tmp_path / "library" / "images" / f"{item_id}.info"
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item["path"], directory / "original.png")
            item_ids.append(item_id)
        return json.dumps({"status": "success", "data": item_ids}).encode()

    connection.getresponse.return_value.read.side_effect = respond
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(eagle.http.client, "HTTPConnection", factory)
    return factory, connection


def test_extension_loads_v3_schema():
    extension = asyncio.run(eagle.comfy_entrypoint())
    assert isinstance(extension, ComfyExtension)
    node, = asyncio.run(extension.get_node_list())
    schema = node.GET_SCHEMA()
    assert schema.is_output_node
    assert not schema.outputs
    assert {item.id for item in schema.inputs} == {"images", "name", "tags", "annotation", "png_data"}


@pytest.mark.parametrize("batch_size,channels", [(1, 3), (2, 4)])
def test_batch_png_metadata_and_cleanup(local_api, tmp_path, batch_size, channels):
    factory, connection = local_api
    images = torch.linspace(-0.2, 1.2, batch_size * 3 * 4 * channels).reshape(batch_size, 3, 4, channels)
    name = "\u753b\u50cf"
    annotation = "\u6ce8\u91c8\nsecond line"
    seen_paths = []

    def receive(method, endpoint, body, headers):
        if method == "GET":
            assert endpoint == "/api/library/info"
            return
        assert method == "POST"
        assert endpoint == "/api/item/addFromPaths"
        assert headers["Content-Type"] == "application/json"
        items = json.loads(body)["items"]
        assert len(items) == batch_size
        for index, item in enumerate(items):
            path = Path(item["path"])
            assert path.is_absolute() and path.parent.parent == tmp_path / "temp"
            seen_paths.append(path)
            assert item["name"] == (name if batch_size == 1 else f"{name}_{index + 1:05}")
            assert item["tags"] == ["\u30bf\u30b0", "test"]
            assert item["annotation"] == annotation
            with Image.open(path) as png:
                assert png.format == "PNG"
                assert png.mode == ("RGB" if channels == 3 else "RGBA")
                assert not png.info
                np.testing.assert_array_equal(
                    np.asarray(png), np.clip(images[index].numpy() * 255, 0, 255).astype(np.uint8),
                )

    connection.request.side_effect = receive
    result = eagle.SendImageToEagle.execute(images, name, " , \u30bf\u30b0, test, , ", annotation)
    assert isinstance(result, io.NodeOutput)
    factory.assert_called_once_with("127.0.0.1", 41595, timeout=60)
    assert connection.request.call_count == 2
    connection.close.assert_called_once()
    assert seen_paths and not any(path.exists() for path in seen_paths)
    assert not list((tmp_path / "temp").iterdir())


def test_image_name_is_not_used_as_a_path(local_api, tmp_path):
    _, connection = local_api
    connection.getresponse.return_value.read.side_effect = None
    connection.getresponse.return_value.read.return_value = b'{"status":"error"}'
    name = r"..\..\outside"
    with pytest.raises(RuntimeError, match="Eagle API failed"):
        eagle.SendImageToEagle.execute(torch.zeros(1, 2, 2, 3), name)
    directory, = (tmp_path / "temp").iterdir()
    path, = directory.iterdir()
    assert path.parent.parent == tmp_path / "temp"
    assert path.name == "image_00001.png"
    assert path.exists()
    assert not (tmp_path / "outside").exists()


def test_png_input_preserves_bytes_and_takes_priority(local_api, tmp_path, monkeypatch):
    buffer = BytesIO()
    Image.new("RGBA", (2, 3), (17, 32, 128, 89)).save(buffer, format="PNG")
    data = buffer.getvalue()
    monkeypatch.setattr(eagle.ui.ImageSaveHelper, "_convert_tensor_to_pil", MagicMock(side_effect=AssertionError("PNG must not be converted")))
    eagle.SendImageToEagle.execute(torch.zeros(2, 2, 2, 3), "saved.png", annotation="Description", png_data=data)
    post = local_api[1].request.call_args
    item, = json.loads(post.kwargs["body"])["items"]
    assert item["name"] == "saved.png"
    assert item["annotation"] == "Description"
    copied, = (tmp_path / "library" / "images").glob("*.info\\original.png")
    assert copied.read_bytes() == data
    assert not Path(item["path"]).exists()


@pytest.mark.parametrize("data", [b"", b"not PNG", "not bytes"])
def test_invalid_png_does_not_fall_back_to_images(local_api, tmp_path, data):
    with pytest.raises(ValueError, match="PNG bytes"):
        eagle.SendImageToEagle.execute(torch.zeros(1, 2, 2, 3), png_data=data)
    local_api[0].assert_not_called()
    assert not (tmp_path / "temp").exists()


def test_missing_image_input_reports_error(local_api):
    with pytest.raises(ValueError, match="Connect images or png_data"):
        eagle.SendImageToEagle.execute()
    local_api[0].assert_not_called()


def test_png_import_failure_keeps_exact_bytes(local_api, tmp_path):
    buffer = BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, format="PNG")
    data = buffer.getvalue()
    local_api[1].request.side_effect = ConnectionRefusedError("refused")
    with pytest.raises(RuntimeError, match="Eagle import failed"):
        eagle.SendImageToEagle.execute(png_data=data)
    path, = (tmp_path / "temp").glob("eagle_*\\image_00001.png")
    assert path.read_bytes() == data


def test_provenance_batch_sockets_preserve_imported_files(local_api, tmp_path, monkeypatch):
    source_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    source_dir.mkdir()
    output_dir.mkdir()
    source = source_dir / "source.png"
    Image.new("RGB", (3, 2), (17, 32, 128)).save(source)
    monkeypatch.setattr(eagle.folder_paths, "get_input_directory", lambda: str(source_dir))
    monkeypatch.setattr(eagle.folder_paths, "get_output_directory", lambda: str(output_dir))
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, "SaveImageWithProvenance", SaveImageWithProvenance)
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, "SendImageToEagle", eagle.SendImageToEagle)
    imported = []

    def respond():
        call = local_api[1].request.call_args
        if call.args[0] == "GET":
            return json.dumps({"status": "success", "data": {"library": {"path": str(tmp_path / "library")}}}).encode()
        item, = json.loads(call.kwargs["body"])["items"]
        item_id = f"import{len(imported)}"
        directory = tmp_path / "library" / "images" / f"{item_id}.info"
        directory.mkdir(parents=True)
        copied = directory / "original.png"
        shutil.copyfile(item["path"], copied)
        imported.append((item, copied))
        return json.dumps({"status": "success", "data": [item_id]}).encode()

    local_api[1].getresponse.return_value.read.side_effect = respond
    prompt = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "source.png"}},
        "2": {"class_type": "ImageBatch", "inputs": {"image1": ["1", 0], "image2": ["1", 0]}},
        "3": {"class_type": "SaveImageWithProvenance", "inputs": {"images": ["2", 0], "filename_prefix": "batch"}},
        "4": {"class_type": "SendImageToEagle", "inputs": {
            "png_data": ["3", 0], "name": ["3", 1], "annotation": ["3", 2], "tags": "provenance",
        }},
    }
    executor = PromptExecutor(Server(), cache_type=False, cache_args={"ram": 0, "ram_inactive": 0})
    executor.execute(prompt, "provenance-eagle", execute_outputs=["4"])
    assert executor.success, executor.status_messages
    previews = executor.history_result["outputs"]["3"]["images"]
    assert len(imported) == len(previews) == 2
    for index, ((item, copied), preview) in enumerate(zip(imported, previews)):
        saved = output_dir / preview["filename"]
        assert copied.read_bytes() == saved.read_bytes()
        assert item["name"] == saved.name
        assert item["tags"] == ["provenance"]
        with Image.open(copied) as image:
            assert item["annotation"] == image.info["Description"]
        manifest, files = read_png_bundle(copied)
        assert set(manifest["graph"]) == {"1", "2", "3"}
        assert manifest["output"]["batch_index"] == index
        assert list(files.values()) == [source.read_bytes()]
        destination = tmp_path / f"restored_{index}"
        assert restore_png(copied, destination) == []
        restored_prompt = json.loads((destination / "prompt.json").read_text("utf-8"))
        assert (destination / restored_prompt["1"]["inputs"]["image"]).read_bytes() == source.read_bytes()
        assert not Path(item["path"]).exists()


@pytest.mark.parametrize(
    "failure",
    ["refused", "timeout", "disconnect", "http", "redirect", "invalid_json", "invalid_encoding", "api", "shape"],
)
def test_import_failures_keep_pngs_without_retry(local_api, tmp_path, failure):
    _, connection = local_api
    response = connection.getresponse.return_value
    if failure == "refused":
        connection.request.side_effect = ConnectionRefusedError("refused")
    elif failure == "timeout":
        response.read.side_effect = TimeoutError("timed out")
    elif failure == "disconnect":
        connection.getresponse.side_effect = http.client.RemoteDisconnected("disconnected")
    elif failure in ("http", "redirect"):
        response.status = 500 if failure == "http" else 302
    else:
        response.read.side_effect = None
        response.read.return_value = {
            "invalid_json": b"not json",
            "invalid_encoding": b"\xff",
            "api": b'{"status": "error"}',
            "shape": b"[]",
        }[failure]
    with pytest.raises(RuntimeError) as caught:
        eagle.SendImageToEagle.execute(torch.zeros(2, 2, 2, 3))
    directory, = (tmp_path / "temp").iterdir()
    assert str(directory) in str(caught.value)
    assert "check Eagle before retrying" in str(caught.value)
    assert caught.value.__cause__ is not None
    for path in directory.iterdir():
        with Image.open(path) as png:
            assert png.size == (2, 2)
    assert len(list(directory.iterdir())) == 2
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def test_save_failure_keeps_prepared_files(local_api, tmp_path, monkeypatch):
    _, connection = local_api
    convert = eagle.ui.ImageSaveHelper._convert_tensor_to_pil
    converted = 0

    def fail_second(image):
        nonlocal converted
        converted += 1
        if converted == 2:
            raise OSError("disk full")
        return convert(image)

    monkeypatch.setattr(eagle.ui.ImageSaveHelper, "_convert_tensor_to_pil", fail_second)
    with pytest.raises(RuntimeError, match="Could not prepare") as caught:
        eagle.SendImageToEagle.execute(torch.zeros(2, 2, 2, 3))
    directory, = (tmp_path / "temp").iterdir()
    assert str(directory) in str(caught.value)
    assert len(list(directory.iterdir())) == 1
    connection.request.assert_not_called()


def test_cleanup_failure_distinguishes_success(local_api, tmp_path, monkeypatch):
    _, connection = local_api
    monkeypatch.setattr(eagle.os, "remove", MagicMock(side_effect=PermissionError("locked")))
    with pytest.raises(RuntimeError, match="import succeeded") as caught:
        eagle.SendImageToEagle.execute(torch.zeros(1, 2, 2, 3))
    directory, = (tmp_path / "temp").iterdir()
    assert str(directory) in str(caught.value)
    assert "retrying will import duplicates" in str(caught.value)
    assert len(list(directory.iterdir())) == 1
    assert connection.request.call_count == 2


def test_copy_confirmation_waits_for_matching_contents(local_api, monkeypatch):
    original_copy = shutil.copyfile
    pending = []

    def partial_copy(source, destination):
        pending.append((source, destination))
        Path(destination).write_bytes(b"\0" * Path(source).stat().st_size)

    def finish_copy(delay):
        assert delay == 0.1
        for source, destination in pending:
            original_copy(source, destination)

    monkeypatch.setattr(shutil, "copyfile", partial_copy)
    sleep = MagicMock(side_effect=finish_copy)
    monkeypatch.setattr(eagle.time, "sleep", sleep)
    eagle.SendImageToEagle.execute(torch.zeros(2, 2, 2, 3))
    sleep.assert_called_once()
    assert all(not Path(source).exists() for source, _ in pending)


def test_copy_timeout_keeps_source_images(local_api, tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "copyfile", lambda source, destination: None)
    monkeypatch.setattr(eagle.time, "monotonic", MagicMock(side_effect=[0, 61]))
    with pytest.raises(RuntimeError, match="did not finish copying") as caught:
        eagle.SendImageToEagle.execute(torch.zeros(1, 2, 2, 3))
    directory, = (tmp_path / "temp").iterdir()
    assert str(directory) in str(caught.value)
    assert len(list(directory.iterdir())) == 1
    assert local_api[1].request.call_count == 2


@pytest.mark.parametrize("ids", [None, [], ["../outside"], ["id", "id"]])
def test_invalid_import_ids_keep_sources(local_api, tmp_path, ids):
    _, connection = local_api
    library = {"status": "success", "data": {"library": {"path": str(tmp_path / "library")}}}
    connection.getresponse.return_value.read.side_effect = [
        json.dumps(library).encode(), json.dumps({"status": "success", "data": ids}).encode(),
    ]
    batch_size = 2 if ids == ["id", "id"] else 1
    with pytest.raises(RuntimeError, match="import ID"):
        eagle.SendImageToEagle.execute(torch.zeros(batch_size, 2, 2, 3))
    directory, = (tmp_path / "temp").iterdir()
    assert len(list(directory.iterdir())) == batch_size


class TestImageSource:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"

    def execute(self):
        return (torch.zeros(1, 2, 2, 3),)


class Server:
    client_id = None

    def send_sync(self, *args, **kwargs):
        pass


def test_identical_queues_import_again_with_execution_cache(local_api, monkeypatch):
    _, connection = local_api
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, "EagleTestImageSource", TestImageSource)
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, "SendImageToEagle", eagle.SendImageToEagle)
    prompt = {
        "source": {"class_type": "EagleTestImageSource", "inputs": {}},
        "send": {
            "class_type": "SendImageToEagle",
            "inputs": {"images": ["source", 0], "name": "test", "tags": "", "annotation": ""},
        },
    }
    executor = PromptExecutor(Server(), cache_type=False, cache_args={"ram": 0, "ram_inactive": 0})
    for index in range(2):
        executor.execute(copy.deepcopy(prompt), f"eagle-test-{index}", execute_outputs=["send"])
        assert executor.success
        assert executor.caches.outputs.get_local("source") is not None
    assert connection.request.call_count == 4
    paths = [
        json.loads(call.kwargs["body"])["items"][0]["path"]
        for call in connection.request.call_args_list if call.args[0] == "POST"
    ]
    assert paths[0] != paths[1]
    assert not any(Path(path).exists() for path in paths)
