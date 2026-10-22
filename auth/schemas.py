from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    user_id: int = Field(gt=0, description="仅供非生产环境模拟登录")
