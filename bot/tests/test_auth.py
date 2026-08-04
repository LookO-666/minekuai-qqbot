"""自动登录中可独立测试的风控识别逻辑。"""

import importlib
import sys
from pathlib import Path


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
    assert not auth._is_image_code_error("短信发送过于频繁")
