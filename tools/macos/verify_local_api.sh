#!/bin/zsh
# Shared authenticated readiness proof for macOS install and repair commands.
# Source this file from a zsh command; it never persists or prints a token.

dian_json_string_field() {
  local document="$1"
  local field="$2"
  print -r -- "$document" | /usr/bin/sed -nE \
    "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\\1/p"
}

dian_verify_local_api() {
  local trust_receipt="$1"
  local expected_version="$2"
  local attempts="${3:-40}"
  local extension_id=""
  local expected_install_id=""
  local receipt_agent_version=""
  local health=""
  local auth_response=""
  local token=""
  local session_install_id=""
  local status_response=""
  local counter=0

  if [[ "$trust_receipt" != *'"ok": true'* || "$trust_receipt" == *'"secret"'* || "$trust_receipt" == *'"access_token"'* ]]; then
    return 1
  fi
  extension_id="$(dian_json_string_field "$trust_receipt" "extension_id")"
  expected_install_id="$(dian_json_string_field "$trust_receipt" "install_id")"
  receipt_agent_version="$(dian_json_string_field "$trust_receipt" "agent_version")"
  if ! print -r -- "$extension_id" | /usr/bin/grep -Eq '^[a-p]{32}$' || \
    ! print -r -- "$expected_install_id" | /usr/bin/grep -Eq '^[a-f0-9]{32}$' || \
    [[ "$receipt_agent_version" != "$expected_version" ]]; then
    return 1
  fi

  while (( counter < attempts )); do
    counter=$((counter + 1))
    health="$(/usr/bin/curl -fsS --max-time 2 http://127.0.0.1:8765/health/live 2>/dev/null || true)"
    if [[ "$health" != *'"status": "ok"'* && "$health" != *'"status":"ok"'* ]]; then
      /bin/sleep 0.5
      continue
    fi
    if [[ "$health" != *"\"version\": \"$expected_version\""* && "$health" != *"\"version\":\"$expected_version\""* ]]; then
      /bin/sleep 0.5
      continue
    fi

    auth_response="$(/usr/bin/curl -fsS --max-time 2 \
      -X POST \
      -H 'Content-Type: application/json' \
      -H 'X-Dian-Agent: 2' \
      -H "X-Dian-Agent-Extension-Id: $extension_id" \
      -H "X-Dian-Agent-Extension-Version: $expected_version" \
      -H "Origin: chrome-extension://$extension_id" \
      --data-binary "{\"extension_id\":\"$extension_id\",\"extension_version\":\"$expected_version\"}" \
      http://127.0.0.1:8765/auth/session 2>/dev/null || true)"
    token="$(dian_json_string_field "$auth_response" "access_token")"
    session_install_id="$(dian_json_string_field "$auth_response" "install_id")"
    auth_response=""
    if [[ "$session_install_id" != "$expected_install_id" ]] || \
      ! print -r -- "$token" | /usr/bin/grep -Eq '^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$'; then
      token=""
      session_install_id=""
      /bin/sleep 0.5
      continue
    fi
    session_install_id=""

    # Keep the token out of argv and disk by passing the header through curl's
    # stdin configuration. The unexported variable is cleared immediately.
    status_response="$(
      {
        print -r -- "header = \"Origin: chrome-extension://$extension_id\""
        print -r -- 'header = "X-Dian-Agent: 2"'
        print -r -- "header = \"X-Dian-Agent-Extension-Id: $extension_id\""
        print -r -- "header = \"X-Dian-Agent-Extension-Version: $expected_version\""
        print -r -- "header = \"X-Dian-Agent-Token: $token\""
      } | /usr/bin/curl --config - -fsS --max-time 3 \
        http://127.0.0.1:8765/auth/status 2>/dev/null || true
    )"
    token=""
    if [[ "$status_response" == *'"authenticated": true'* || "$status_response" == *'"authenticated":true'* ]] && \
      [[ "$status_response" == *"\"agent_version\": \"$expected_version\""* || "$status_response" == *"\"agent_version\":\"$expected_version\""* ]] && \
      [[ "$status_response" == *"\"session_extension_version\": \"$expected_version\""* || "$status_response" == *"\"session_extension_version\":\"$expected_version\""* ]]; then
      status_response=""
      extension_id=""
      expected_install_id=""
      receipt_agent_version=""
      return 0
    fi
    status_response=""
    /bin/sleep 0.5
  done

  token=""
  auth_response=""
  status_response=""
  extension_id=""
  expected_install_id=""
  receipt_agent_version=""
  return 1
}
