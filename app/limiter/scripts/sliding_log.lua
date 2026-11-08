-- Sliding window log, executed atomically inside Redis.
-- KEYS[1] = sorted set of this client's request times, KEYS[2] = counter that keeps entries unique
-- ARGV[1] = limit (requests per window), ARGV[2] = window length (ms), ARGV[3] = cost of this request

local key = KEYS[1]
local seq_key = KEYS[2]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- Forget requests that have slid out of the window, then count the rest.
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window_ms)
local count = redis.call('ZCARD', key)

local allowed = 0
local retry_after_ms = 0
if count + cost <= limit then
  -- Log this request. Two requests can share a millisecond, so each entry gets a sequence number.
  for i = 1, cost do
    local seq = redis.call('INCR', seq_key)
    redis.call('ZADD', key, now, now .. '-' .. seq)
  end
  count = count + cost
  allowed = 1
  -- Both keys can go once this newest request has left the window.
  redis.call('PEXPIRE', key, window_ms)
  redis.call('PEXPIRE', seq_key, window_ms)
else
  -- Wait until enough of the oldest requests slide out of the window.
  local must_leave = count + cost - limit
  local entry = redis.call('ZRANGE', key, must_leave - 1, must_leave - 1, 'WITHSCORES')
  if entry[2] then
    retry_after_ms = tonumber(entry[2]) + window_ms - now
  else
    retry_after_ms = window_ms
  end
end

-- The full allowance is back when the newest logged request leaves the window.
local reset_ms = 0
local newest = redis.call('ZRANGE', key, -1, -1, 'WITHSCORES')
if newest[2] then
  reset_ms = tonumber(newest[2]) + window_ms - now
end

return {allowed, limit - count, retry_after_ms, reset_ms}
