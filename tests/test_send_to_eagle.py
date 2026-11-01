import asyncio
import copy
import http.client
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import torch
from PIL import Image

import nodes
from comfy_api.latest import ComfyExtension, io
from custom_nodes import comfyui_to_eagle as eagle
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
    assert {item.id for item in schema.inputs} == {"images", "name", "tags", "annotation"}


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
