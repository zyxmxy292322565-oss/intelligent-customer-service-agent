from cache.limiter import TokenBucketLimiter


def main():
    limiter = TokenBucketLimiter(capacity=5, rate=1)
    for i in range(10):
        result = limiter.allow("rate_limit:agent:user:1001")
        print(i, result)


if __name__ == "__main__":
    main()
