"""旧入口兼容层；不再读取可伪造的 user-id 请求头。"""

from fastapi import Request

from auth.dependency import get_current_user


def get_current_user_id(request: Request) -> int:
    return get_current_user(request)
