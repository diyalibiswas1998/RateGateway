-- Sliding window counter, executed atomically inside Redis.
-- KEYS[1] = hash holding this client's window number and two counts
-- ARGV[1] = limit (requests per window), ARGV[2] = window length (ms), ARGV[3] = cost of this request

local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- Time is cut into fixed windows numbered 0, 1, 2, ...; find ours and how far into it we are.
local window = math.floor(now / window_ms)
local elapsed = now - window * window_ms

-- Load the saved counts and roll them forward if time has moved on.
local state = redis.call('HMGET', key, 'window', 'curr', 'prev')
local saved_window = tonumber(state[1])
local curr = tonumber(state[2]) or 0
local prev = tonumber(state[3]) or 0
if saved_window == nil or saved_window < window - 1 then
  -- New client, or idle for over a whole window: nothing counts any more.
  prev = 0
  curr = 0
elseif saved_window == window - 1 then
  -- One window boundary passed: the current count becomes the previous one.
  prev = curr
  curr = 0
end

-- Estimate the requests in the last window_ms: all of this window, plus
-- the share of the previous window that still overlaps it.
local weight = (window_ms - elapsed) / window_ms
local estimate = prev * weight + curr

local allowed = 0
local retry_after_ms = 0
if estimate + cost <= limit then
  curr = curr + cost
  estimate = estimate + cost
  allowed = 1
else
  local room = limit - cost - curr
  if room >= 0 then
    -- Wait until the previous window's share shrinks enough.
    retry_after_ms = math.ceil(window_ms * (1 - room / prev) - elapsed)
  else
    -- This window alone is full. In the next one its count becomes the previous
    -- count, so also wait for that share to shrink enough.
    local next_wait = 0
    if curr > 0 then
      next_wait = math.max(0, window_ms * (1 - (limit - cost) / curr))
    end
    retry_after_ms = math.ceil(window_ms - elapsed + next_wait)
  end
end

-- After two idle windows both counts are irrelevant, so the key can go.
redis.call('HSET', key, 'window', window, 'curr', curr, 'prev', prev)
redis.call('PEXPIRE', key, 2 * window_ms)

-- Everything is forgotten once this window and the next have passed.
local reset_ms = window_ms - elapsed
if curr > 0 then
  reset_ms = reset_ms + window_ms
end

return {allowed, math.max(0, math.floor(limit - estimate)), retry_after_ms, reset_ms}
