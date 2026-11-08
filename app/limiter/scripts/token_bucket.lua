-- Token bucket, executed atomically inside Redis.
-- KEYS[1] = the bucket key for one client
-- ARGV[1] = capacity, ARGV[2] = refill rate (tokens/sec), ARGV[3] = cost of this request

local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

-- Redis server time: {seconds, microseconds}. Convert to milliseconds.
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- Load the saved state. A brand new client gets a full bucket.
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

-- Lazy refill: add tokens for the time that passed, never above capacity.
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill_rate / 1000)

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  -- How long until enough tokens drip in?
  retry_after_ms = math.ceil((cost - tokens) * 1000 / refill_rate)
end

-- Save state. Expire the key once the bucket would be full again:
-- a missing key and a full bucket mean the same thing, so idle clients cost no memory.
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity * 1000 / refill_rate))

local reset_ms = math.ceil((capacity - tokens) * 1000 / refill_rate)
return {allowed, math.floor(tokens), retry_after_ms, reset_ms}
