# sshfleet:collect
# One round-trip per host per refresh. Prints exactly one JSON object and always exits 0 so a
# partial result still reaches the UI.
#
# Deliberately dependency-free: no jq, no python on the remote side. Scalars are printed with
# printf/awk; container data is passed through in the runtime's own JSON. Every external command
# is wrapped in `timeout` so one hung daemon cannot stall the refresh.
#
# $1 = probe list, space separated "port:path" (empty for none)
# $2 = runtime override: docker | podman | none | auto
# $3 = "1" to use sudo, "0" for a rootless runtime (podman run as an ordinary user)
# $4 = directory whose subdirectories are static sites (their mtimes are reported as deploys)
# $5 = "1" to discover served domains from nginx/apache/caddy/traefik/haproxy configs

export LC_ALL=C
PROBES="${1:-}"
RT_OVERRIDE="${2:-auto}"
USE_SUDO="${3:-1}"
WEB_ROOT="${4:-/var/www/html}"
DISCOVER="${5:-0}"   # 1 = also read the web servers' configs for the domains this host serves

json_str() {
    local s=${1//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/}
    s=${s//$'\t'/\\t}
    printf '"%s"' "$s"
}

# ---------------------------------------------------------------- system
read -r up_raw _ < /proc/uptime 2>/dev/null || up_raw=0
read -r l1 l5 l15 _ < /proc/loadavg 2>/dev/null || { l1=0; l5=0; l15=0; }
ncpu=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 1)
mem_total=$(awk '/^MemTotal:/{print $2*1024; exit}' /proc/meminfo 2>/dev/null || echo 0)
mem_avail=$(awk '/^MemAvailable:/{print $2*1024; exit}' /proc/meminfo 2>/dev/null || echo 0)
kernel=$(uname -r 2>/dev/null || echo unknown)

disks=$(df -P -B1 / /var/lib/docker /var/lib/containers /home 2>/dev/null | awk '
    NR>1 && !seen[$6]++ {
        pct=$5; sub(/%/,"",pct);
        printf "%s{\"mount\":\"%s\",\"size\":%s,\"used\":%s,\"pct\":%s}", (n++?",":""), $6, $2, $3, pct
    }')

# ---------------------------------------------------------------- runtime
case "$RT_OVERRIDE" in
    docker|podman) RT="$RT_OVERRIDE" ;;
    none) RT="" ;;
    *) RT=$(command -v podman 2>/dev/null || command -v docker 2>/dev/null || true) ;;
esac
RT_NAME=$(basename "${RT:-none}")
[ "$RT_NAME" = "none" ] && RT=""

SUDO=""
sudo_ok=1
if [ -n "$RT" ] && [ "$USE_SUDO" = "1" ]; then
    if sudo -n true 2>/dev/null; then
        SUDO="sudo -n"
    else
        sudo_ok=0
    fi
fi

containers='[]'
inspect=''
runtime_error=''
if [ -n "$RT" ] && [ "$sudo_ok" = "1" ]; then
    if ps_out=$(timeout 15 $SUDO "$RT" ps -a --format json 2>&1); then
        case "$ps_out" in
            \[*)  containers="$ps_out" ;;                                  # podman: already an array
            \{*)  containers="[$(printf '%s\n' "$ps_out" | paste -sd, -)]" ;;  # docker: one object per line
            "")   containers='[]' ;;
            *)    containers='[]'; runtime_error="$ps_out" ;;
        esac
    else
        runtime_error="$ps_out"
    fi

    ids=$(timeout 10 $SUDO "$RT" ps -aq 2>/dev/null)
    if [ -n "$ids" ]; then
        # One inspect for every container: TSV is cheaper to parse than nested JSON and the
        # fields differ between docker and podman only in the log path.
        inspect=$(timeout 15 $SUDO "$RT" inspect --format \
            '{{.Name}}	{{.Config.Image}}	{{.Image}}	{{.Created}}	{{.State.StartedAt}}	{{.State.ExitCode}}	{{.RestartCount}}	{{.HostConfig.NetworkMode}}	{{.LogPath}}' \
            $ids 2>/dev/null)
        if [ -z "$inspect" ]; then
            inspect=$(timeout 15 $SUDO "$RT" inspect --format \
                '{{.Name}}	{{.Config.Image}}	{{.Image}}	{{.Created}}	{{.State.StartedAt}}	{{.State.ExitCode}}	{{.RestartCount}}	{{.HostConfig.NetworkMode}}	{{.HostConfig.LogConfig.Path}}' \
                $ids 2>/dev/null)
        fi
    fi
fi

# ---------------------------------------------------------------- network
listening=$(ss -ltnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -un | paste -sd, -)
[ -z "$listening" ] && listening=$(netstat -ltn 2>/dev/null | awk 'NR>2{print $4}' | sed 's/.*://' | sort -un | paste -sd, -)

probes=''
for spec in $PROBES; do
    port=${spec%%:*}
    path=${spec#*:}
    [ "$path" = "$port" ] && path="/"
    code=""
    if command -v curl >/dev/null 2>&1; then
        # Capture only; a failed curl still prints 000, and `|| echo 000` would append a second one.
        code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}${path}" 2>/dev/null)
        [ -z "$code" ] && code="000"
    else
        if timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/${port}" 2>/dev/null; then code="open"; else code="closed"; fi
    fi
    probes="${probes:+$probes,}{\"port\":${port},\"path\":$(json_str "$path"),\"code\":$(json_str "$code")}"
done

# ------------------------------------------------------- static frontends
frontends=$(find "$WEB_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\t%T@\n' 2>/dev/null | awk -F'\t' '
    {printf "%s{\"name\":\"%s\",\"mtime\":%d}", (n++?",":""), $1, $2}')
# is-active exits non-zero for "inactive"; capture the word and ignore the status.
nginx=$(systemctl is-active nginx 2>/dev/null)
[ -z "$nginx" ] && nginx="absent"

# ------------------------------------------------------------------ sites
# The domains this host serves, read out of whatever is configured to serve them. Only the name
# and the port of its block are taken; nothing else in those files is read. Names that are not
# addresses — `_`, localhost, a bare IP, anything with a variable in it — are dropped, so what
# comes back is a list you could paste into a browser.
#
# Costed on a real host at about 15 ms, but it is still the one part of a refresh that reads a
# tree of files rather than asking a daemon a question, so it runs only when asked for: at
# startup and on a manual check, never on the timer. Each block costs one `[ -d ]` on a host
# that does not run that tool.
sites=''
emit_sites() {  # $1 = tool, reads name<TAB>port<TAB>ssl on stdin
    awk -v tool="$1" -F'\t' '
        $1 != "" && !seen[$1 FS $2] { seen[$1 FS $2] = 1
            printf "%s%s\t%s\t%s\t%s", (NR > 1 ? "\n" : ""), $1, $2, $3, tool }'
}

if [ "$DISCOVER" = "1" ]; then
    # nginx: server blocks, tracking brace depth so a `location` does not end the block early.
    if [ -d /etc/nginx ]; then
        found=$(find /etc/nginx/sites-enabled /etc/nginx/conf.d /etc/nginx/nginx.conf \
            -maxdepth 1 \( -type f -o -type l \) 2>/dev/null | head -100)
        [ -n "$found" ] && sites=$sites$(timeout 5 awk '
            function flush(  n, i, a, nm) {
                if (names != "") {
                    n = split(names, a, " ")
                    for (i = 1; i <= n; i++) {
                        nm = a[i]
                        if (nm == "_" || nm == "localhost" || nm ~ /\$/ || nm ~ /^[0-9.]+$/) continue
                        sub(/^\*\./, "", nm)
                        print nm "\t" (port == "" ? "80" : port) "\t" ssl
                    }
                }
                names = ""; port = ""; ssl = "0"
            }
            {
                line = $0; sub(/#.*/, "", line)
                opens = gsub(/\{/, "{", line); closes = gsub(/\}/, "}", line)
                if (!inserver && line ~ /(^|[ \t;}])server[ \t]*\{/) {
                    inserver = 1; serverdepth = depth; names = ""; port = ""; ssl = "0"
                }
                if (inserver) {
                    t = line
                    while (match(t, /(^|[;{])[ \t]*server_name[ \t]+[^;]*/)) {
                        v = substr(t, RSTART, RLENGTH); sub(/^[^a-z]*server_name[ \t]+/, "", v)
                        names = names " " v; t = substr(t, RSTART + RLENGTH)
                    }
                    t = line
                    while (match(t, /(^|[;{])[ \t]*listen[ \t]+[^;]*/)) {
                        v = substr(t, RSTART, RLENGTH); sub(/^[^a-z]*listen[ \t]+/, "", v)
                        if (v ~ /ssl/) ssl = "1"
                        split(v, p, /[ \t]+/); a = p[1]; sub(/.*:/, "", a)
                        if (a ~ /^[0-9]+$/ && port == "") port = a
                        t = substr(t, RSTART + RLENGTH)
                    }
                }
                depth += opens - closes
                if (inserver && depth <= serverdepth) { flush(); inserver = 0 }
            }
        ' $found 2>/dev/null | emit_sites nginx)
    fi

    # Apache: ServerName and ServerAlias, with the port off the <VirtualHost> tag.
    for d in /etc/apache2/sites-enabled /etc/httpd/conf.d; do
        [ -d "$d" ] || continue
        found=$(find "$d" -maxdepth 1 \( -type f -o -type l \) 2>/dev/null | head -100)
        [ -n "$found" ] && sites=$sites$'\n'$(timeout 5 awk '
            /<VirtualHost/ { p = $0; sub(/.*:/, "", p); sub(/[^0-9].*/, "", p); port = (p == "" ? "80" : p) }
            /<\/VirtualHost/ { port = "" }
            /^[ \t]*Server(Name|Alias)[ \t]/ {
                t = $0; sub(/^[ \t]*Server(Name|Alias)[ \t]+/, "", t); sub(/#.*/, "", t)
                n = split(t, a, /[ \t]+/)
                for (i = 1; i <= n; i++) {
                    nm = a[i]; sub(/^\*\./, "", nm)
                    if (nm == "" || nm == "localhost" || nm ~ /^[0-9.]+$/ || nm ~ /\$/) continue
                    pt = (port == "" ? "80" : port)
                    print nm "\t" pt "\t" (pt == "443" ? "1" : "0")
                }
            }
        ' $found 2>/dev/null | emit_sites apache)
    done

    # Caddy: a site block opens with its addresses, which may carry a scheme and a port.
    if [ -f /etc/caddy/Caddyfile ]; then
        sites=$sites$'\n'$(timeout 5 awk '
            /^[ \t]*[#{]/ { next }
            /\{[ \t]*$/ && depth == 0 {
                t = $0; sub(/\{.*/, "", t); n = split(t, a, /[ \t,]+/)
                for (i = 1; i <= n; i++) {
                    nm = a[i]; ssl = "1"; port = "443"
                    if (nm ~ /^http:\/\//) { ssl = "0"; port = "80" }
                    sub(/^https?:\/\//, "", nm)
                    if (nm ~ /:[0-9]+$/) { port = nm; sub(/.*:/, "", port); sub(/:[0-9]+$/, "", nm); ssl = (port == "443" ? "1" : ssl) }
                    sub(/^\*\./, "", nm)
                    if (nm == "" || nm == "localhost" || nm ~ /^[0-9.]+$/ || nm ~ /^:/ ) continue
                    print nm "\t" port "\t" ssl
                }
            }
            { depth += gsub(/\{/, "{") - gsub(/\}/, "}") }
        ' /etc/caddy/Caddyfile 2>/dev/null | emit_sites caddy)
    fi

    # Traefik and HAProxy name their hosts inside a routing rule rather than a block of their own.
    if [ -d /etc/traefik ]; then
        found=$(find /etc/traefik -maxdepth 2 -type f 2>/dev/null | head -50)
        [ -n "$found" ] && sites=$sites$'\n'$(timeout 5 grep -ho "Host(\`[^\`]*\`)" $found 2>/dev/null |
            sed 's/Host(`//; s/`)//' | awk '{ print $1 "\t443\t1" }' | emit_sites traefik)
    fi
    if [ -f /etc/haproxy/haproxy.cfg ]; then
        sites=$sites$'\n'$(timeout 5 awk '
            /hdr\(host\)/ { for (i = 1; i <= NF; i++) if ($i ~ /\./ && $i !~ /[()]/ && $i !~ /^-/) print $i "\t443\t1" }
        ' /etc/haproxy/haproxy.cfg 2>/dev/null | emit_sites haproxy)
    fi

    # One name may come from several files; keep the first sighting of each name and port.
    sites=$(printf '%s' "$sites" | awk -F'\t' 'NF >= 3 && $1 != "" && !s[$1 FS $2]++ { printf "%s%s", (n++ ? "\n" : ""), $0 }')
fi

printf '{"v":1,"host":%s,"now":%s,"uptime_s":%s,"kernel":%s,"load":[%s,%s,%s],"cpus":%s,' \
    "$(json_str "$(hostname 2>/dev/null)")" "$(date +%s)" "${up_raw%%.*}" "$(json_str "$kernel")" \
    "$l1" "$l5" "$l15" "$ncpu"
printf '"mem_total":%s,"mem_avail":%s,"disks":[%s],' "$mem_total" "$mem_avail" "$disks"
printf '"runtime":%s,"sudo_ok":%s,"runtime_error":%s,' "$(json_str "$RT_NAME")" "$sudo_ok" "$(json_str "$runtime_error")"
printf '"containers":%s,"inspect":%s,' "$containers" "$(json_str "$inspect")"
printf '"listening":[%s],"probes":[%s],"frontends":[%s],"nginx":%s,"sites":%s}\n' \
    "$listening" "$probes" "$frontends" "$(json_str "$nginx")" "$(json_str "$sites")"
