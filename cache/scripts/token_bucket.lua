local tokens = tonumber(redis.call("HGET", KEYS[1], "tokens"))
local last_time = tonumber(redis.call("HGET", KEYS[1], "last_time"))

local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])


-- 第一次请求初始化
if tokens == nil then
    tokens = capacity
    last_time = now
end


-- 计算经过时间
local elapsed = now - last_time


-- 补充token
local add_tokens = elapsed * rate

tokens = math.min(
    capacity,
    tokens + add_tokens
)


-- 判断是否允许

if tokens < 1 then

    redis.call(
        "HSET",
        KEYS[1],
        "tokens",
        tokens,
        "last_time",
        now
    )
    redis.call("EXPIRE", KEYS[1], ttl)

    return 0

end


-- 消耗一个token

tokens = tokens - 1


redis.call(
    "HSET",
    KEYS[1],
    "tokens",
    tokens,
    "last_time",
    now
)
redis.call("EXPIRE", KEYS[1], ttl)


return 1
