#!/bin/sh
set -eu

INSTANCE="${1:-default}"
PIDFILE="/run/accel-pppd-${INSTANCE}.pid"
STATE_DIR="/run/accel-ppp-vpp"
STATE_FILE="${STATE_DIR}/${INSTANCE}.state"
CONFIG_FILE="/etc/accel-ppp-${INSTANCE}.conf"

get_pppoe_interface() {
  [ -f "$CONFIG_FILE" ] || return 0
  sed -n 's/^interface=\([^,]*\).*/\1/p' "$CONFIG_FILE" | head -1
}

get_vpp_ifindex() {
  iface="$1"
  [ -n "$iface" ] || return 0
  /usr/bin/vppctl show interface 2>/dev/null | awk -v iface="$iface" '$1 == iface { print $2; exit }'
}

cleanup_vpp_sessions_for_instance() {
  pppoe_if="$(get_pppoe_interface)"
  encap_if_index="$(get_vpp_ifindex "$pppoe_if")"
  [ -n "$encap_if_index" ] || return 0

  /usr/bin/vppctl show pppoe session 2>/dev/null | awk -v encap="$encap_if_index" '
    /client-ip4/ {
      ip=""; sid=""; eif="";
      for (i=1; i<=NF; i++) {
        if ($i == "client-ip4") ip=$(i+1);
        else if ($i == "session-id") sid=$(i+1);
        else if ($i == "encap-if-index") eif=$(i+1);
      }
      getline;
      mac="";
      for (i=1; i<=NF; i++) {
        if ($i == "client-mac") mac=$(i+1);
      }
      if (eif == encap && ip != "" && sid != "" && mac != "")
        print ip "|" sid "|" mac "|" eif;
    }
  ' | while IFS='|' read -r client_ip session_id client_mac eif; do
    /usr/bin/vppctl "create pppoe session client-ip ${client_ip} session-id ${session_id} client-mac ${client_mac} encap-if-index ${eif} del" >/dev/null 2>&1 || true
    remove_kernel_artifacts "$client_ip"
  done
}

running_instance_count() {
  count=0
  for file in /run/accel-pppd-*.pid; do
    [ -e "$file" ] || continue
    pid="$(cat "$file" 2>/dev/null || true)"
    [ -n "$pid" ] || continue
    if kill -0 "$pid" 2>/dev/null; then
      count=$((count + 1))
    fi
  done
  printf '%s\n' "$count"
}

current_instance_pid() {
  if [ -f "$PIDFILE" ]; then
    cat "$PIDFILE" 2>/dev/null || true
  fi
}

other_active_instances() {
  current_pid="$(current_instance_pid)"
  count=0
  for file in /run/accel-pppd-*.pid; do
    [ -e "$file" ] || continue
    pid="$(cat "$file" 2>/dev/null || true)"
    [ -n "$pid" ] || continue
    if [ -n "$current_pid" ] && [ "$pid" = "$current_pid" ] && [ "$file" = "$PIDFILE" ]; then
      continue
    fi
    if kill -0 "$pid" 2>/dev/null; then
      count=$((count + 1))
    fi
  done
  printf '%s\n' "$count"
}

remove_kernel_artifacts() {
  ip_addr="$1"

  if [ -n "$ip_addr" ] && [ "$ip_addr" != "0.0.0.0" ]; then
    /sbin/ip route del "${ip_addr}/32" proto 201 >/dev/null 2>&1 || true

    while IFS= read -r neigh_line; do
      dev="$(printf '%s\n' "$neigh_line" | sed -n 's/.* dev \([^ ]*\) .*/\1/p')"
      [ -n "$dev" ] || continue
      /sbin/ip neigh del "$ip_addr" dev "$dev" >/dev/null 2>&1 || true
    done <<EOF
$(/sbin/ip neigh show to "$ip_addr" 2>/dev/null || true)
EOF
  fi
}

cleanup_state_entries() {
  [ -f "$STATE_FILE" ] || return 0

  while IFS='|' read -r sw_if_index session_id client_ip client_mac session_name policer_down policer_up; do
    [ -n "$sw_if_index" ] || continue

    # ---- Step 1: Unbind policers from the interface ----
    # This must happen BEFORE deleting the PPPoE session or the policer
    # objects, otherwise worker threads still route packets through the
    # policer-input node with a stale/freed index → SIGSEGV.
    if [ -n "$policer_down" ]; then
      /usr/bin/vppctl "policer output ${session_name} del" >/dev/null 2>&1 || true
    fi
    if [ -n "$policer_up" ]; then
      /usr/bin/vppctl "policer input ${session_name} del" >/dev/null 2>&1 || true
    fi

    # ---- Step 2: Delete policer objects ----
    if [ -n "$policer_down" ]; then
      /usr/bin/vppctl "policer del name ${policer_down}" >/dev/null 2>&1 || true
    fi
    if [ -n "$policer_up" ]; then
      /usr/bin/vppctl "policer del name ${policer_up}" >/dev/null 2>&1 || true
    fi

    # ---- Step 3: Delete PPPoE session ----
    if [ -n "$client_ip" ] && [ -n "$session_id" ] && [ -n "$client_mac" ]; then
      encap_if_index="$(/usr/bin/vppctl show pppoe session 2>/dev/null | awk -v ip="$client_ip" -v sid="$session_id" -v mac="$client_mac" '
        $0 ~ /client-ip4/ && $0 ~ ip && $0 ~ "session-id " sid {
          for (i = 1; i <= NF; i++) {
            if ($i == "encap-if-index") {
              print $(i+1)
              exit
            }
          }
        }
      ')"
      if [ -n "$encap_if_index" ]; then
        /usr/bin/vppctl "create pppoe session client-ip ${client_ip} session-id ${session_id} client-mac ${client_mac} encap-if-index ${encap_if_index} del" >/dev/null 2>&1 || true
      fi
    fi

    # ---- Step 4: Clean up kernel artifacts ----
    remove_kernel_artifacts "$client_ip"
  done < "$STATE_FILE"

  rm -f "$STATE_FILE"
}

cleanup_sync_state() {
  active_instances="$(other_active_instances)"
  if [ "$active_instances" -le 0 ]; then
    rm -f /run/pppoe-neigh-sync/state.json >/dev/null 2>&1 || true
  fi
}

mkdir -p "$STATE_DIR"

# Startup safety: only perform targeted cleanup for this instance.
# Never issue global VPP cleanup while other accel instances may still own sessions.
cleanup_vpp_sessions_for_instance
cleanup_state_entries
cleanup_sync_state
