"""自动登录中可独立测试的风控识别逻辑。"""

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "minekuai"))
auth = importlib.import_module("auth")


def test_detects_unusual_location_sms_requirement():
    assert auth._is_sms_required(
        500, "非常用地点登录，请使用手机号验证码登录！"
    )


def test_does_not_treat_bad_password_as_sms_requirement():
    assert not auth._is_sms_required(500, "用户名或密码错误")


def test_extracts_base64_payload_from_data_uri():
    assert auth._extract_image_base64("data:image/png;base64,QUJD") == "QUJD"


def test_image_code_error_recognizes_captcha_message():
    assert auth._is_image_code_error("图形验证码计算错误")
    assert auth._is_image_code_error("Captcha invalid")
    assert not auth._is_image_code_error("短信发送过于频繁")


@pytest.mark.asyncio
@pytest.mark.parametrize("has_login, has_script, has_elements, blocked", [
    (False, True, False, True),
    (False, False, True, True),
    (True, True, True, False),
    (False, False, False, False),
])
async def test_site_verification_detection(
    has_login, has_script, has_elements, blocked,
):
    class Page:
        def locator(self, selector):
            if "mkl-phone" in selector:
                count = has_login
            elif selector.startswith("script"):
                count = has_script
            else:
                count = has_elements
            from types import SimpleNamespace
            return SimpleNamespace(count=AsyncMock(return_value=int(count)))

    if blocked:
        with pytest.raises(auth.LoginError, match="网站安全验证页"):
            await auth._check_site_verification(Page())
    else:
        await auth._check_site_verification(Page())


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", [
    "_fill_phone", "_fill_password", "_fill_sms_phone",
    "_fill_image_code", "_fill_sms_code",
])
async def test_fill_failures_do_not_disclose_input(helper):
    sensitive_input = "827361"

    class FailingLocator:
        @property
        def first(self):
            return self

        async def fill(self, value, **kwargs):
            raise RuntimeError(f'Call log: fill("{value}")')

    class Page:
        def locator(self, *args, **kwargs):
            return FailingLocator()

        get_by_placeholder = locator

    with pytest.raises(auth.LoginError) as exc:
        await getattr(auth, helper)(Page(), sensitive_input)
    assert sensitive_input not in str(exc.value)


@pytest.mark.asyncio
async def test_image_retry_then_sms_login(monkeypatch):
    import asyncio

    login_responses = asyncio.Queue()
    sms_responses = asyncio.Queue()
    for response in (
        {"status": 200, "body": {"code": 500, "msg": "Captcha invalid"}},
        {"status": 200, "body": {"code": 200}},
    ):
        sms_responses.put_nowait(response)
    login_responses.put_nowait({
        "status": 200,
        "body": {"code": 200, "data": {"access_token": "test-token"}},
    })

    class Image:
        first = None

        def __init__(self):
            self.first = self
            self.click = AsyncMock()

        async def wait_for(self, **kwargs):
            pass

        async def count(self):
            return 0

        async def get_attribute(self, name):
            return "data:image/png;base64,QUJD"

    class Page:
        def __init__(self):
            self.image = Image()
            self.old_sources = []

        def locator(self, selector):
            return self.image

        async def wait_for_function(self, expression, *, arg, timeout):
            self.old_sources.append(arg)

    page = Page()
    for helper in (
        "_switch_to_sms_tab", "_fill_sms_phone", "_click_send_sms_button",
        "_fill_image_code", "_click_confirm_send_sms", "_fill_sms_code",
        "_click_login_button",
    ):
        monkeypatch.setattr(auth, helper, AsyncMock())
    kinds = []

    async def provide(challenge):
        kinds.append(challenge.kind)
        return "123456" if challenge.kind == "sms" else "7"

    result = await auth._complete_sms_login(
        page, "test-phone", provide, login_responses, sms_responses, 100
    )
    assert result["data"]["access_token"] == "test-token"
    assert kinds == ["image", "image", "sms"]
    assert page.old_sources == ["", "data:image/png;base64,QUJD"]
    page.image.click.assert_not_awaited()
