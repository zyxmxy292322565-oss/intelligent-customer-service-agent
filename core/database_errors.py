"""Safe HTTP classification only; never retries or changes persistence state."""
import uuid
import logging
import pymysql
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

async def database_error_response(request, exc):
    number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    transient = number in {0, 1040, 1205, 1213, 2002, 2003, 2006, 2013} or isinstance(exc, pymysql.err.InterfaceError)
    read_only = request.method in {'GET', 'HEAD'} or request.url.path == '/auth/login'
    trace_id = str(uuid.uuid4())
    if transient and read_only:
        status, code, message = 503, 'DATABASE_UNAVAILABLE', '数据服务暂时不可用，请稍后重试。'
    elif transient:
        status, code, message = 503, 'DATABASE_RESULT_UNCERTAIN', '暂时无法核实请求的保存结果，请查询原记录，勿重复提交操作。'
    else:
        status, code, message = 500, 'DATABASE_ERROR', '数据服务异常，请联系管理员并提供追踪编号。'
    logger.error('database request error errno=%s class=%s trace_id=%s', number, type(exc).__name__, trace_id)
    return JSONResponse(status_code=status, content={
        'success': False, 'code': code, 'message': message, 'trace_id': trace_id,
        'data': {'retry_safe': bool(transient and read_only), 'persistence_confirmed': False},
    }, headers={'Retry-After': '2'} if transient and read_only else None)
