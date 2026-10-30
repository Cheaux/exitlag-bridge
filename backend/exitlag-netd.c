/*
 * exitlag-netd -- native Linux side of the ExitLag compatibility backend.
 *
 * It watches candidate outbound TCP connections via NFQUEUE, builds the flow
 * identity, attributes the owning Linux process, and asks the Windows-side shim
 * to run ExitLag's real tcpConnectRequest callback. Observation-only is the
 * default. With --apply-tcp, a valid IPv4 destination mutation is installed as
 * one exact, short-lived 4-tuple -> address:port nftables map element before the
 * queued SYN is accepted. The later output NAT chain then applies that result
 * and conntrack owns the rest of the flow.
 *
 * Design constraints honoured:
 *   - fail open: any error, timeout, missing shim or malformed reply results in
 *     NF_ACCEPT. Losing the helper must never black-hole host networking.
 *   - no persistent firewall state: rules live in a dedicated `inet exitlag`
 *     table installed/removed by backend/nft-setup.sh and nft-teardown.sh.
 *   - NFQUEUE is bound with a bounded queue and the kernel rules use
 *     `queue ... bypass`, so if this process dies packets flow normally.
 *
 * Talks NFQUEUE over libmnl directly (libnetfilter_queue is not required).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <libmnl/libmnl.h>
#include <linux/netfilter.h>
#include <linux/netfilter/nfnetlink.h>
#include <linux/netfilter/nfnetlink_queue.h>
#include <limits.h>
#include <netinet/in.h>
#include <netinet/ip.h>
#include <netinet/ip6.h>
#include <netinet/tcp.h>
#include <netinet/udp.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "protocol.h"

/*
 * Wine hands the socket fd to the application only after wineserver has created
 * it, so immediately after connect() the sole holder is wineserver and the real
 * game process is invisible. The SYN is already queued, so waiting here only
 * delays connection setup slightly, and only for flows whose sole owner is Wine
 * plumbing -- ordinary Linux processes resolve on the first attempt. The wait
 * actually taken is logged so the real distribution can be measured instead of
 * assumed.
 */
/* The shim resolves the true owner via GetExtendedTcpTable (Windows PIDs), so
   this /proc lookup is only a diagnostic aid and a fallback for non-Wine
   processes. It must stay cheap: waiting here delays the SYN. */
#define ATTR_MAX_ATTEMPTS 2
#define ATTR_STEP_MS      5

static volatile sig_atomic_t g_stop;
static int   g_queue_num = 0;
static int   g_udp_queue_num = -1;
static int   g_observe_udp;
static int   g_listen_fd = -1;
static int   g_shim_fd   = -1;
static unsigned int g_shim_windows_pid;
static unsigned long long g_next_tag = 1;
static unsigned long long g_next_endpoint;   /* seeded per run, never reused */
static unsigned long long g_flow_index;
static struct timespec g_last_flow;
static int   g_reply_timeout_ms = 300;
static int   g_no_queue;          /* IPC-only mode: never touch netfilter */
/*
 * The helper runs as root and applies destination rewrites supplied over its
 * IPC socket, so an unauthenticated peer could redirect this machine's
 * outbound connections. Only root and the desktop user who owns the ExitLag
 * process may connect; everyone else is refused at accept().
 */
static uid_t g_allowed_peer_uid = (uid_t)-1;

/*
 * Per-session IPC token. The uid check stops other accounts; it cannot stop
 * another process of the same user, which is the remaining impersonation gap.
 * The token closes it: it is generated fresh on every start, written to a file
 * only the owning user can read, and must be presented in the handshake before
 * any routing command is served.
 *
 * It is never logged, never written anywhere but that one file, and the file is
 * removed on exit. A token from a previous session is useless because a new one
 * is generated each start.
 */
static unsigned char g_session_token[EXL_TOKEN_BYTES];
static char g_token_path[PATH_MAX];
static int g_shim_authenticated;
/*
 * ExitLag redirects to its own local proxy: every redirect observed in testing
 * targeted 127.0.0.1. Allowing an arbitrary unicast destination would let an
 * authenticated peer steer this machine's traffic off-box, which is far more
 * power than the proven runtime needs. Loopback-only by default;
 * --allow-remote-redirect re-opens it for deliberate experiments.
 */
static int g_allow_remote_redirect;
/* Test-only: lets an IPC-only harness resolve PIDs the helper never reported.
   The production runners never pass it. */
static int g_allow_any_pid_resolve;
/* PIDs the helper itself reported to the shim, so PID lookups cannot be used to
   enumerate unrelated processes on the machine. */
#define OBSERVED_PID_MAX 256
static unsigned int g_observed_pids[OBSERVED_PID_MAX];
static unsigned int g_observed_pid_next;

static void remember_observed_pid(unsigned int pid)
{
    if (!pid) return;
    for (unsigned int i = 0; i < OBSERVED_PID_MAX; ++i)
        if (g_observed_pids[i] == pid) return;
    g_observed_pids[g_observed_pid_next++ % OBSERVED_PID_MAX] = pid;
}

static int pid_was_observed(unsigned int pid)
{
    for (unsigned int i = 0; i < OBSERVED_PID_MAX; ++i)
        if (g_observed_pids[i] == pid) return 1;
    return 0;
}
static int   g_apply_tcp;         /* opt-in: arm exact per-flow nft DNAT */
static int   g_apply_udp;         /* opt-in: execute one proven UDP post */
static int   g_udp_active_once;   /* lock active UDP to one eligible endpoint */
static unsigned int g_udp_active_index = 1; /* 1-based eligible endpoint ordinal */
static unsigned int g_udp_eligible_endpoint_count;
static unsigned long long g_udp_active_endpoint;
static const char *g_udp_exe;     /* optional active/observation process gate */
static const char *g_selftest;    /* synthesize one flow to this IPv4 dest */
static FILE *g_log;

/*
 * Production process-identity translation.  The key is either an exact host
 * executable path (/proc/<pid>/exe) or an exact argv token naming a Windows
 * image (the form exposed by Wine/Proton).  The value is the Windows-style
 * image path ExitLag already associates with a configured application.
 *
 * A deliberately small, fixed table keeps this path allocation-free while
 * packets are suspended in NFQUEUE.  Configuration errors fail helper startup
 * instead of silently broadening attribution.
 */
#define IDENTITY_MAP_MAX 128
#define IDENTITY_WINEPREFIX_TAG "wineprefix:"
struct identity_map_entry {
    char source[PATH_MAX];
    char target[512];
};
static struct identity_map_entry g_identity_map[IDENTITY_MAP_MAX];
static unsigned int g_identity_map_count;
static int g_identity_map_enabled;

#define UDP_ENDPOINT_MAX 2048
#define UDP_REMOTE_RULE_MAX 16
#define UDP_POST_PAYLOAD_MAX 65507
#define UDP_REINJECT_MARK 0x584c0001u
struct udp_endpoint {
    int used;
    int family;
    int created_sent;
    int connect_sent;
    unsigned long long id;
    unsigned long inode;
    int linux_pid;
    unsigned int effective_pid;
    unsigned char local_address[28];
    unsigned char first_remote[28];
    unsigned int filtering_flag;
    unsigned int remote_rule_count;
    unsigned int remote_rule_unique_count;
    unsigned char remote_rules[UDP_REMOTE_RULE_MAX][28];
    unsigned int flowctl_handle;
    unsigned int active_ordinal;
    unsigned long long datagrams_out;
    unsigned long long datagrams_in;
    struct timespec last_seen;
    char exe[256];
};
static struct udp_endpoint g_udp_endpoints[UDP_ENDPOINT_MAX];

struct udp_post_capture {
    unsigned int count;
    int valid;
    unsigned int api;
    int status;
    unsigned char remote_address[28];
    unsigned int option_flags;
    unsigned int options_length;
    unsigned int payload_length;
    unsigned char payload[UDP_POST_PAYLOAD_MAX];
};
static struct udp_post_capture g_udp_post;

static struct udp_endpoint *udp_endpoint_by_id(unsigned long long id)
{
    for (unsigned int i = 0; i < UDP_ENDPOINT_MAX; ++i)
        if (g_udp_endpoints[i].used && g_udp_endpoints[i].id == id) return &g_udp_endpoints[i];
    return NULL;
}

static int udp_endpoint_has_remote(const struct udp_endpoint *endpoint,
                                   const unsigned char remote[28])
{
    for (unsigned int i = 0; i < endpoint->remote_rule_unique_count; ++i)
        if (memcmp(endpoint->remote_rules[i], remote, 28) == 0) return 1;
    return 0;
}

/*
 * Bound the log. A long session with a chatty game can otherwise produce
 * hundreds of megabytes; observed 11 MB from a single ExitLag session during
 * development. One previous generation is kept so a crash is still
 * investigable, and rotation is checked cheaply rather than on every line.
 */
#define LOG_MAX_BYTES_DEFAULT (8L * 1024 * 1024)
static const char *g_log_path;
static long g_log_max_bytes = LOG_MAX_BYTES_DEFAULT;

static void log_rotate_if_needed(void)
{
    if (!g_log || !g_log_path) return;
    static unsigned int since_check;
    if (++since_check < 256) return;      /* ftell is cheap, but not free */
    since_check = 0;
    long size = ftell(g_log);
    if (size < 0 || size < g_log_max_bytes) return;
    char rotated[PATH_MAX];
    snprintf(rotated, sizeof(rotated), "%s.1", g_log_path);
    fflush(g_log);
    fclose(g_log);
    rename(g_log_path, rotated);          /* replaces any previous generation */
    g_log = fopen(g_log_path, "a");
    if (g_log) {
        fprintf(g_log, "--- log rotated at %ld bytes; previous generation is %s ---\n",
                size, rotated);
        fflush(g_log);
    }
}

static void log_line(const char *fmt, ...)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tm;
    gmtime_r(&ts.tv_sec, &tm);
    char stamp[64];
    strftime(stamp, sizeof(stamp), "%Y-%m-%dT%H:%M:%S", &tm);

    va_list ap;
    va_start(ap, fmt);
    FILE *out = g_log ? g_log : stdout;
    fprintf(out, "%s.%03ldZ ", stamp, ts.tv_nsec / 1000000);
    vfprintf(out, fmt, ap);
    fputc('\n', out);
    fflush(out);
    va_end(ap);
    log_rotate_if_needed();
}

static void on_signal(int sig) { (void)sig; g_stop = 1; }

/* ---------------------------------------------------------------- process */

/*
 * Attribute a local endpoint to a PID: find the socket inode in /proc/net/tcp*
 * for the exact local address:port, then find which process holds that inode.
 * Best effort by design -- an unattributed flow is still reported, with pid 0.
 */
static unsigned long find_socket_inode_in(const char *path, int hex_family,
                                          const unsigned char *local_ip, unsigned short local_port);

/*
 * Attribute an IPv4 flow whose socket is dual-stack. A process that opens an
 * AF_INET6 socket without IPV6_V6ONLY -- which is the default for Java, and
 * common elsewhere -- carries IPv4 traffic on a socket the kernel lists in
 * /proc/net/tcp6 as ::ffff:a.b.c.d. Looking only in /proc/net/tcp therefore
 * finds nothing and the flow is reported unattributed, which is what happened
 * to Minecraft.
 */
static unsigned long find_socket_inode(int family, const unsigned char *local_ip, unsigned short local_port)
{
    if (family == AF_INET) {
        unsigned long inode = find_socket_inode_in("/proc/net/tcp", AF_INET, local_ip, local_port);
        if (inode) return inode;
        unsigned char mapped[16] = { 0,0,0,0, 0,0,0,0, 0,0,0xff,0xff, 0,0,0,0 };
        memcpy(mapped + 12, local_ip, 4);
        return find_socket_inode_in("/proc/net/tcp6", AF_INET6, mapped, local_port);
    }
    return find_socket_inode_in("/proc/net/tcp6", AF_INET6, local_ip, local_port);
}

static unsigned long find_socket_inode_in(const char *path, int family,
                                          const unsigned char *local_ip, unsigned short local_port)
{
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char line[512];
    unsigned long inode = 0;
    if (!fgets(line, sizeof(line), f)) { fclose(f); return 0; }  /* header */
    while (fgets(line, sizeof(line), f)) {
        char local[128], rem[128];
        unsigned long ino = 0;
        int st = 0;
        if (sscanf(line, "%*d: %127[0-9A-Fa-f:] %127[0-9A-Fa-f:] %X %*x:%*x %*x:%*x %*x %*d %*d %lu",
                   local, rem, &st, &ino) < 4)
            continue;
        char *colon = strrchr(local, ':');
        if (!colon) continue;
        *colon = 0;
        unsigned int port = (unsigned int)strtoul(colon + 1, NULL, 16);
        if (port != local_port) continue;

        unsigned char addr[16];
        size_t hexlen = strlen(local);
        if (hexlen != (family == AF_INET ? 8u : 32u)) continue;
        for (size_t i = 0; i < hexlen / 2; ++i) {
            char byte[3] = { local[i * 2], local[i * 2 + 1], 0 };
            addr[i] = (unsigned char)strtoul(byte, NULL, 16);
        }
        /* /proc prints each 32-bit word little-endian */
        int match = 1;
        for (size_t w = 0; w < hexlen / 8; ++w)
            for (int b = 0; b < 4; ++b)
                if (addr[w * 4 + b] != local_ip[w * 4 + (3 - b)]) { match = 0; break; }
        if (match) { inode = ino; break; }
    }
    fclose(f);
    return inode;
}

/* UDP sockets are commonly bound to ANY, while packets carry the interface
   address selected by the route. Prefer an exact local-address match and fall
   back to an all-zero bind on the same port. */
static unsigned long find_udp_socket_inode_in(const char *path, int family,
                                              const unsigned char *local_ip,
                                              unsigned short local_port);

/* Same dual-stack case as TCP: an AF_INET6 socket without IPV6_V6ONLY carries
   IPv4 datagrams and is listed in /proc/net/udp6 as ::ffff:a.b.c.d. */
static unsigned long find_udp_socket_inode(int family, const unsigned char *local_ip,
                                           unsigned short local_port)
{
    if (family == AF_INET) {
        unsigned long inode = find_udp_socket_inode_in("/proc/net/udp", AF_INET, local_ip, local_port);
        if (inode) return inode;
        unsigned char mapped[16] = { 0,0,0,0, 0,0,0,0, 0,0,0xff,0xff, 0,0,0,0 };
        memcpy(mapped + 12, local_ip, 4);
        return find_udp_socket_inode_in("/proc/net/udp6", AF_INET6, mapped, local_port);
    }
    return find_udp_socket_inode_in("/proc/net/udp6", AF_INET6, local_ip, local_port);
}

static unsigned long find_udp_socket_inode_in(const char *path, int family,
                                              const unsigned char *local_ip,
                                              unsigned short local_port)
{
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char line[512];
    unsigned long wildcard_inode = 0;
    if (!fgets(line, sizeof(line), f)) { fclose(f); return 0; }
    while (fgets(line, sizeof(line), f)) {
        char local[128], rem[128];
        unsigned long inode = 0;
        int st = 0;
        if (sscanf(line, "%*d: %127[0-9A-Fa-f:] %127[0-9A-Fa-f:] %X %*x:%*x %*x:%*x %*x %*d %*d %lu",
                   local, rem, &st, &inode) < 4)
            continue;
        char *colon = strrchr(local, ':');
        if (!colon) continue;
        *colon = 0;
        if ((unsigned int)strtoul(colon + 1, NULL, 16) != local_port) continue;

        unsigned char addr[16] = { 0 };
        size_t hexlen = strlen(local);
        if (hexlen != (family == AF_INET ? 8u : 32u)) continue;
        for (size_t i = 0; i < hexlen / 2; ++i) {
            char byte[3] = { local[i * 2], local[i * 2 + 1], 0 };
            addr[i] = (unsigned char)strtoul(byte, NULL, 16);
        }
        int exact = 1, any = 1;
        for (size_t w = 0; w < hexlen / 8; ++w) {
            for (int b = 0; b < 4; ++b) {
                unsigned char value = addr[w * 4 + b];
                if (value != local_ip[w * 4 + (3 - b)]) exact = 0;
                if (value) any = 0;
            }
        }
        if (exact) { fclose(f); return inode; }
        if (any && !wildcard_inode) wildcard_inode = inode;
    }
    fclose(f);
    return wildcard_inode;
}

static int udp_inode_present_in(const char *path, unsigned long wanted);

/* An IPv4 endpoint on a dual-stack socket lives in /proc/net/udp6, so checking
   only /proc/net/udp would report it gone and retire it early. */
static int udp_inode_present(int family, unsigned long wanted)
{
    if (!wanted) return 1; /* unknown ownership: retire by idle timeout instead */
    if (family == AF_INET)
        return udp_inode_present_in("/proc/net/udp", wanted) ||
               udp_inode_present_in("/proc/net/udp6", wanted);
    return udp_inode_present_in("/proc/net/udp6", wanted);
}

static int udp_inode_present_in(const char *path, unsigned long wanted)
{
    FILE *f = fopen(path, "r");
    if (!f) return 1;
    char line[512];
    if (!fgets(line, sizeof(line), f)) { fclose(f); return 1; }
    while (fgets(line, sizeof(line), f)) {
        unsigned long inode = 0;
        if (sscanf(line, "%*d: %*s %*s %*X %*x:%*x %*x:%*x %*x %*d %*d %lu", &inode) == 1 &&
            inode == wanted) { fclose(f); return 1; }
    }
    fclose(f);
    return 0;
}

/*
 * Map a socket inode to the PID that really owns it.
 *
 * Wine duplicates every application socket into `wineserver`, so a naive
 * first-match-wins scan over /proc fd tables credits nearly all Wine traffic to
 * wineserver and the game process is never seen. Candidates are therefore all
 * collected and ranked:
 *
 *   best  a Windows image (argv contains ".exe") that is not wineserver
 *   next  any other process that is not wineserver/wine
 *   last  wineserver itself, only if nothing else holds the inode
 */
static int process_is_wine_plumbing(const char *pid_str)
{
    char path[320], comm[64] = "";
    snprintf(path, sizeof(path), "/proc/%s/comm", pid_str);
    FILE *f = fopen(path, "r");
    if (f) { if (!fgets(comm, sizeof(comm), f)) comm[0] = 0; fclose(f); }
    comm[strcspn(comm, "\n")] = 0;
    return !strcmp(comm, "wineserver") || !strcmp(comm, "wine") || !strcmp(comm, "wine64");
}

static int process_is_windows_image(const char *pid_str)
{
    char path[320];
    snprintf(path, sizeof(path), "/proc/%s/cmdline", pid_str);
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char buf[4096];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = 0;
    for (size_t i = 0; i < n; ) {
        if (strstr(buf + i, ".exe")) return 1;
        i += strlen(buf + i) + 1;
    }
    return 0;
}

/* Diagnostic: every process found holding the inode, with its rank. */
static char g_last_candidates[512];

static int pid_is_wine_plumbing_num(int pid)
{
    char buf[32];
    snprintf(buf, sizeof(buf), "%d", pid);
    return process_is_wine_plumbing(buf);
}

static int pid_for_inode(unsigned long inode, char *exe_out, size_t exe_len)
{
    if (!inode) return 0;
    char want[64];
    snprintf(want, sizeof(want), "socket:[%lu]", inode);

    DIR *proc = opendir("/proc");
    if (!proc) return 0;

    int best = 0, best_rank = -1;
    g_last_candidates[0] = 0;
    struct dirent *de;
    while ((de = readdir(proc))) {
        if (de->d_name[0] < '0' || de->d_name[0] > '9') continue;
        char fdpath[320];
        snprintf(fdpath, sizeof(fdpath), "/proc/%s/fd", de->d_name);
        DIR *fds = opendir(fdpath);
        if (!fds) continue;

        int holds = 0;
        struct dirent *fde;
        while (!holds && (fde = readdir(fds))) {
            char link[640], target[128];
            snprintf(link, sizeof(link), "%s/%s", fdpath, fde->d_name);
            ssize_t n = readlink(link, target, sizeof(target) - 1);
            if (n <= 0) continue;
            target[n] = 0;
            if (!strcmp(target, want)) holds = 1;
        }
        closedir(fds);
        if (!holds) continue;

        int rank;
        if (process_is_wine_plumbing(de->d_name)) rank = 0;
        else if (process_is_windows_image(de->d_name)) rank = 2;
        else rank = 1;

        {
            char comm[64] = "";
            char cpath[320];
            snprintf(cpath, sizeof(cpath), "/proc/%s/comm", de->d_name);
            FILE *cf = fopen(cpath, "r");
            if (cf) { if (fgets(comm, sizeof(comm), cf)) comm[strcspn(comm, "\n")] = 0; fclose(cf); }
            size_t used = strlen(g_last_candidates);
            snprintf(g_last_candidates + used, sizeof(g_last_candidates) - used,
                     "%s%s:%s:r%d", used ? "," : "", de->d_name, comm, rank);
        }
        if (rank > best_rank) { best_rank = rank; best = atoi(de->d_name); }
        if (rank == 2) break;                    /* cannot do better */
    }
    closedir(proc);

    exe_out[0] = 0;
    if (best) {
        char exelink[320];
        snprintf(exelink, sizeof(exelink), "/proc/%d/exe", best);
        ssize_t m = readlink(exelink, exe_out, exe_len - 1);
        exe_out[m > 0 ? m : 0] = 0;
    }
    return best;
}

/* ------------------------------------------------------------------- ipc */

static int send_frame(int fd, unsigned short type, const void *payload, unsigned int len)
{
    struct exl_header head = { EXL_MAGIC, type, 0, len };
    const unsigned char *parts[2] = { (const unsigned char *)&head, payload };
    size_t lengths[2] = { sizeof(head), len };
    for (int part = 0; part < 2; ++part) {
        size_t sent = 0;
        while (sent < lengths[part]) {
            ssize_t n = write(fd, parts[part] + sent, lengths[part] - sent);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) return -1;
            sent += (size_t)n;
        }
    }
    return 0;
}

static int read_exact(int fd, void *buf, size_t len, int timeout_ms)
{
    unsigned char *p = buf;
    while (len) {
        struct pollfd pfd = { fd, POLLIN, 0 };
        int r = poll(&pfd, 1, timeout_ms);
        if (r <= 0) return -1;
        ssize_t n = read(fd, p, len);
        if (n <= 0) return -1;
        p += n; len -= (size_t)n;
    }
    return 0;
}

/* Constant-time compare: no early exit that could leak a prefix match. */
static int token_equal(const unsigned char *a, const unsigned char *b)
{
    unsigned char diff = 0;
    for (unsigned int i = 0; i < EXL_TOKEN_BYTES; ++i) diff |= (unsigned char)(a[i] ^ b[i]);
    return diff == 0;
}

static void token_remove(void)
{
    if (g_token_path[0]) unlink(g_token_path);
}

/*
 * Generate this session's token and publish it where only the owning user can
 * read it. Written via a private temporary file and renamed, so a reader never
 * observes a half-written token, and created with 0600 before any content is
 * written so it is never briefly world-readable.
 */
static int token_publish(uid_t owner)
{
    int rnd = open("/dev/urandom", O_RDONLY);
    if (rnd < 0) { log_line("cannot open /dev/urandom: %s", strerror(errno)); return -1; }
    size_t got = 0;
    while (got < sizeof(g_session_token)) {
        ssize_t n = read(rnd, g_session_token + got, sizeof(g_session_token) - got);
        if (n <= 0) { close(rnd); log_line("short read from /dev/urandom"); return -1; }
        got += (size_t)n;
    }
    close(rnd);

    char tmp[PATH_MAX + 8];
    snprintf(tmp, sizeof(tmp), "%s.tmp", g_token_path);
    unlink(tmp);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0) { log_line("cannot create token file %s: %s", tmp, strerror(errno)); return -1; }
    if (fchmod(fd, 0600) != 0) { close(fd); unlink(tmp); return -1; }
    if (geteuid() == 0 && owner != (uid_t)-1 && fchown(fd, owner, (gid_t)-1) != 0) {
        log_line("cannot give the token file to uid %u: %s", (unsigned)owner, strerror(errno));
        close(fd); unlink(tmp); return -1;
    }
    char hex[EXL_TOKEN_BYTES * 2 + 2];
    for (unsigned int i = 0; i < EXL_TOKEN_BYTES; ++i)
        snprintf(hex + i * 2, 3, "%02x", g_session_token[i]);
    hex[EXL_TOKEN_BYTES * 2] = '\n';
    ssize_t written = write(fd, hex, sizeof(hex) - 1);
    close(fd);
    if (written != (ssize_t)(sizeof(hex) - 1)) { unlink(tmp); log_line("short write to token file"); return -1; }
    if (rename(tmp, g_token_path) != 0) {
        log_line("cannot publish token file %s: %s", g_token_path, strerror(errno));
        unlink(tmp);
        return -1;
    }
    /* Deliberately logs the path and never the value. */
    log_line("IPC session token published at %s (owner uid %u, mode 0600)",
             g_token_path, (unsigned)(owner == (uid_t)-1 ? geteuid() : owner));
    return 0;
}

/*
 * Owning uid of the process at the other end of an accepted loopback TCP
 * connection. The peer's socket appears in /proc/net/tcp{,6} as the row whose
 * local address is the peer's and whose remote address is ours; column 8 of
 * that row is the uid the kernel recorded for its owner.
 */
static int peer_uid_from_proc(const char *path, unsigned short peer_port,
                              unsigned short our_port, uid_t *out)
{
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[512];
    if (!fgets(line, sizeof(line), f)) { fclose(f); return -1; }   /* header */
    while (fgets(line, sizeof(line), f)) {
        char local[128], remote[128];
        unsigned int state, uid;
        /* sl local rem st tx:rx tr:when retrnsmt uid */
        if (sscanf(line, "%*d: %127s %127s %x %*x:%*x %*x:%*x %*x %u",
                   local, remote, &state, &uid) != 4) continue;
        const char *lcolon = strrchr(local, ':');
        const char *rcolon = strrchr(remote, ':');
        if (!lcolon || !rcolon) continue;
        unsigned int lport = 0, rport = 0;
        if (sscanf(lcolon + 1, "%x", &lport) != 1) continue;
        if (sscanf(rcolon + 1, "%x", &rport) != 1) continue;
        if (lport == peer_port && rport == our_port) { *out = (uid_t)uid; fclose(f); return 0; }
    }
    fclose(f);
    return -1;
}

static int peer_uid_of_connection(int fd, uid_t *out)
{
    struct sockaddr_storage peer, local;
    socklen_t peer_len = sizeof(peer), local_len = sizeof(local);
    if (getpeername(fd, (struct sockaddr *)&peer, &peer_len) != 0) return -1;
    if (getsockname(fd, (struct sockaddr *)&local, &local_len) != 0) return -1;
    unsigned short peer_port, our_port;
    if (peer.ss_family == AF_INET) {
        peer_port = ntohs(((struct sockaddr_in *)&peer)->sin_port);
        our_port  = ntohs(((struct sockaddr_in *)&local)->sin_port);
    } else if (peer.ss_family == AF_INET6) {
        peer_port = ntohs(((struct sockaddr_in6 *)&peer)->sin6_port);
        our_port  = ntohs(((struct sockaddr_in6 *)&local)->sin6_port);
    } else return -1;
    if (peer_uid_from_proc("/proc/net/tcp", peer_port, our_port, out) == 0) return 0;
    return peer_uid_from_proc("/proc/net/tcp6", peer_port, our_port, out);
}

static void drop_shim(const char *why)
{
    if (g_shim_fd >= 0) { close(g_shim_fd); g_shim_fd = -1; }
    g_shim_windows_pid = 0;
    g_shim_authenticated = 0;
    log_line("shim disconnected (%s); failing open", why);
}

static int load_identity_map(const char *map_path)
{
    FILE *f = fopen(map_path, "r");
    if (!f) {
        log_line("identity map: cannot open %s: %s", map_path, strerror(errno));
        return -1;
    }

    char line[PATH_MAX + 768];
    unsigned int line_number = 0;
    while (fgets(line, sizeof(line), f)) {
        ++line_number;
        size_t length = strlen(line);
        if (length && line[length - 1] != '\n' && !feof(f)) {
            log_line("identity map: line %u is too long", line_number);
            fclose(f);
            return -1;
        }
        while (length && (line[length - 1] == '\n' || line[length - 1] == '\r'))
            line[--length] = 0;
        if (!length || line[0] == '#') continue;

        char *tab = strchr(line, '\t');
        if (!tab || tab == line || !tab[1] || strchr(tab + 1, '\t')) {
            log_line("identity map: line %u must be SOURCE<TAB>WINDOWS_PATH", line_number);
            fclose(f);
            return -1;
        }
        *tab++ = 0;
        if (g_identity_map_count == IDENTITY_MAP_MAX) {
            log_line("identity map: more than %u entries", IDENTITY_MAP_MAX);
            fclose(f);
            return -1;
        }

        char canonical[PATH_MAX], prefixed[PATH_MAX + sizeof(IDENTITY_WINEPREFIX_TAG)];
        const char *source = line;
        if (!strncmp(line, IDENTITY_WINEPREFIX_TAG, sizeof(IDENTITY_WINEPREFIX_TAG) - 1)) {
            /* A Wine/Proton game's socket is owned by that prefix's wineserver,
               whose /proc exe and argv are identical for every prefix on the
               machine. The prefix path is the only thing that distinguishes
               them, so it is matched against the owner's WINEPREFIX. */
            const char *dir = line + sizeof(IDENTITY_WINEPREFIX_TAG) - 1;
            struct stat st;
            if (dir[0] != '/' || !realpath(dir, canonical) ||
                stat(canonical, &st) != 0 || !S_ISDIR(st.st_mode)) {
                log_line("identity map: line %u wineprefix source is not an existing directory: %s",
                         line_number, dir);
                fclose(f);
                return -1;
            }
            snprintf(prefixed, sizeof(prefixed), "%s%s", IDENTITY_WINEPREFIX_TAG, canonical);
            source = prefixed;
        } else if (line[0] == '/') {
            if (!realpath(line, canonical)) {
                log_line("identity map: line %u source does not resolve: %s", line_number, line);
                fclose(f);
                return -1;
            }
            source = canonical;
        } else if (!strstr(line, ".exe") && !strstr(line, ".EXE")) {
            log_line("identity map: line %u source must be an absolute host path, an exact .exe argv "
                     "token, or " IDENTITY_WINEPREFIX_TAG "<dir>", line_number);
            fclose(f);
            return -1;
        }

        if (strlen(source) >= sizeof(g_identity_map[0].source) ||
            strlen(tab) >= sizeof(g_identity_map[0].target)) {
            log_line("identity map: line %u source or target is too long", line_number);
            fclose(f);
            return -1;
        }
        for (unsigned int i = 0; i < g_identity_map_count; ++i) {
            if (!strcmp(g_identity_map[i].source, source)) {
                log_line("identity map: duplicate source on line %u: %s", line_number, source);
                fclose(f);
                return -1;
            }
        }

        struct identity_map_entry *entry = &g_identity_map[g_identity_map_count++];
        snprintf(entry->source, sizeof(entry->source), "%s", source);
        snprintf(entry->target, sizeof(entry->target), "%s", tab);
    }
    if (ferror(f)) {
        log_line("identity map: read failed for %s", map_path);
        fclose(f);
        return -1;
    }
    fclose(f);
    g_identity_map_enabled = 1;
    log_line("identity map: loaded %u exact entries from %s (strict mode)",
             g_identity_map_count, map_path);
    for (unsigned int i = 0; i < g_identity_map_count; ++i)
        log_line("identity map: %s -> %s", g_identity_map[i].source, g_identity_map[i].target);
    return 0;
}

/*
 * A Wine/Proton game does not hold its own socket: the fd belongs to that
 * prefix's wineserver, which reaches this code as the owning PID. Every
 * wineserver on the machine has the same /proc exe and argv, so the prefix it
 * serves -- taken from its WINEPREFIX environment -- is what identifies it.
 * Proton uses one prefix per title, which makes this exactly the right
 * granularity there.
 */
static const char *mapped_identity_for_wineprefix(unsigned int pid)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/%u/environ", pid);
    int fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    static char environ_buf[65536];
    ssize_t n = read(fd, environ_buf, sizeof(environ_buf) - 1);
    close(fd);
    if (n <= 0) return NULL;
    environ_buf[n] = 0;

    static const char key[] = "WINEPREFIX=";
    for (ssize_t at = 0; at < n; ) {
        const char *entry = environ_buf + at;
        size_t entry_length = strlen(entry);
        if (!strncmp(entry, key, sizeof(key) - 1)) {
            char canonical[PATH_MAX], tagged[PATH_MAX + sizeof(IDENTITY_WINEPREFIX_TAG)];
            if (realpath(entry + sizeof(key) - 1, canonical)) {
                snprintf(tagged, sizeof(tagged), "%s%s", IDENTITY_WINEPREFIX_TAG, canonical);
                for (unsigned int i = 0; i < g_identity_map_count; ++i)
                    if (!strcmp(tagged, g_identity_map[i].source)) return g_identity_map[i].target;
            }
            return NULL;   /* it has a prefix and none matched */
        }
        at += (ssize_t)entry_length + 1;
    }
    return NULL;
}

static const char *mapped_identity_for_pid(unsigned int pid)
{
    char path[64], value[PATH_MAX];
    snprintf(path, sizeof(path), "/proc/%u/exe", pid);
    ssize_t n = readlink(path, value, sizeof(value) - 1);
    if (n > 0) {
        value[n] = 0;
        for (unsigned int i = 0; i < g_identity_map_count; ++i)
            if (!strcmp(value, g_identity_map[i].source)) return g_identity_map[i].target;
    }

    snprintf(path, sizeof(path), "/proc/%u/cmdline", pid);
    FILE *f = fopen(path, "r");
    if (!f) return NULL;
    char command[4096];
    n = (ssize_t)fread(command, 1, sizeof(command) - 1, f);
    fclose(f);
    command[n] = 0;
    for (size_t at = 0; at < (size_t)n; ) {
        const char *arg = command + at;
        for (unsigned int i = 0; i < g_identity_map_count; ++i)
            if (!strcmp(arg, g_identity_map[i].source)) return g_identity_map[i].target;
        at += strlen(arg) + 1;
    }

    return mapped_identity_for_wineprefix(pid);
}


/*
 * Resolve a Linux PID to the executable path we can most honestly report.
 *
 * For a Wine process /proc/<pid>/exe points at the ELF loader, not the Windows
 * image, so the argv is searched for the actual *.exe. A trailing ".so" (Wine's
 * `Tibia.exe.so` form of the same binary) is dropped. Nothing is synthesised: if
 * no path can be read the answer is empty and the caller fails closed.
 */
static int resolve_pid_path(unsigned int pid, char *out, size_t out_len)
{
    out[0] = 0;
    char path[64];

    if (g_identity_map_enabled) {
        const char *mapped = mapped_identity_for_pid(pid);
        if (mapped) {
            snprintf(out, out_len, "%s", mapped);
            return 2; /* explicit identity translation */
        }

        /* Strict mode: an arbitrary native argv containing a .exe-looking
           token must not impersonate a configured game.  Unmapped processes
           retain their genuine host executable and therefore fail open. */
        snprintf(path, sizeof(path), "/proc/%u/exe", pid);
        ssize_t n = readlink(path, out, out_len - 1);
        if (n > 0) { out[n] = 0; return 1; }
        return 0;
    }

    snprintf(path, sizeof(path), "/proc/%u/cmdline", pid);
    FILE *f = fopen(path, "r");
    if (f) {
        char buf[4096];
        size_t n = fread(buf, 1, sizeof(buf) - 1, f);
        fclose(f);
        buf[n] = 0;
        for (size_t i = 0; i < n; ) {
            const char *arg = buf + i;
            size_t len = strlen(arg);
            const char *dot = strstr(arg, ".exe");
            if (dot && (dot[4] == 0 || !strcmp(dot, ".exe.so"))) {
                snprintf(out, out_len, "%s", arg);
                char *tail = strstr(out, ".exe.so");
                if (tail) tail[4] = 0;          /* Tibia.exe.so -> Tibia.exe */
                return 1;
            }
            i += len + 1;
        }
    }

    snprintf(path, sizeof(path), "/proc/%u/exe", pid);
    ssize_t n = readlink(path, out, out_len - 1);
    if (n > 0) { out[n] = 0; return 1; }
    out[0] = 0;
    return 0;
}

static void serve_resolve_pid(const unsigned char *payload, unsigned int length)
{
    if (length < sizeof(struct exl_resolve_pid_req)) return;
    const struct exl_resolve_pid_req *req = (const struct exl_resolve_pid_req *)payload;
    char exe[512];
    /* Only PIDs this helper actually reported may be resolved. Otherwise the
       lookup is a general-purpose "what is process N" oracle for anything on
       the machine, which the runtime never needs. */
    if (!g_allow_any_pid_resolve && !pid_was_observed(req->process_id)) {
        struct exl_resolve_pid_reply reply;
        reply.tag = req->tag; reply.path_length = 0; reply.reserved = 0;
        send_frame(g_shim_fd, EXL_MSG_RESOLVE_PID_REPLY, &reply, sizeof(reply));
        log_line("resolve pid=%u REFUSED (not a pid this helper reported)", req->process_id);
        return;
    }
    int resolution = resolve_pid_path(req->process_id, exe, sizeof(exe));
    unsigned int len = resolution ? (unsigned int)strlen(exe) : 0;

    unsigned char frame[sizeof(struct exl_resolve_pid_reply) + 512];
    struct exl_resolve_pid_reply *reply = (struct exl_resolve_pid_reply *)frame;
    reply->tag = req->tag;
    reply->path_length = len;
    reply->reserved = 0;
    if (len) memcpy(frame + sizeof(*reply), exe, len);
    send_frame(g_shim_fd, EXL_MSG_RESOLVE_PID_REPLY, frame, (unsigned int)(sizeof(*reply) + len));
    log_line("resolve pid=%u identity=%s -> %s", req->process_id,
             resolution == 2 ? "translated" : resolution == 1 ? "genuine" : "unresolved",
             len ? exe : "(unresolved)");
}

static const struct exl_udp_datagram_req *g_current_udp_req;
static const unsigned char *g_current_udp_payload;

static unsigned long long payload_hash(const unsigned char *data, unsigned int length)
{
    unsigned long long hash = 1469598103934665603ULL;
    for (unsigned int i = 0; i < length; ++i) { hash ^= data[i]; hash *= 1099511628211ULL; }
    return hash;
}

static void hex_head(char *out, size_t out_len, const unsigned char *data, unsigned int length)
{
    static const char digits[] = "0123456789abcdef";
    unsigned int shown = length < 32 ? length : 32;
    size_t at = 0;
    for (unsigned int i = 0; i < shown && at + 2 < out_len; ++i) {
        out[at++] = digits[data[i] >> 4]; out[at++] = digits[data[i] & 15];
    }
    if (length > shown && at + 3 < out_len) { out[at++] = '.'; out[at++] = '.'; out[at++] = '.'; }
    out[at] = 0;
}

static void wire_endpoint(char *out, size_t out_len, const unsigned char address[28])
{
    unsigned short family = (unsigned short)(address[0] | ((unsigned short)address[1] << 8));
    unsigned short port_net;
    memcpy(&port_net, address + 2, sizeof(port_net));
    char ip[INET6_ADDRSTRLEN] = "?";
    if (family == 2) inet_ntop(AF_INET, address + 4, ip, sizeof(ip));
    else if (family == 23) inet_ntop(AF_INET6, address + 8, ip, sizeof(ip));
    snprintf(out, out_len, "%s%s%s:%u", family == 23 ? "[" : "", ip,
             family == 23 ? "]" : "", (unsigned)ntohs(port_net));
}

static void handle_udp_post(const unsigned char *payload, unsigned int length)
{
    if (length < sizeof(struct exl_udp_post)) { log_line("udp post: malformed short frame"); return; }
    const struct exl_udp_post *post = (const struct exl_udp_post *)payload;
    unsigned int trailing = length - (unsigned int)sizeof(*post);
    if (post->options_length > trailing || post->payload_length > trailing - post->options_length) {
        log_line("udp post: malformed lengths event_tag=%llu", post->event_tag); return;
    }
    const unsigned char *bytes = payload + sizeof(*post) + post->options_length;
    char remote[96], head[72];
    wire_endpoint(remote, sizeof(remote), post->remote_address);
    hex_head(head, sizeof(head), bytes, post->payload_length);
    int same_event = g_current_udp_req && post->event_tag == g_current_udp_req->tag &&
                     post->endpoint_id == g_current_udp_req->endpoint_id;
    int same_remote = same_event &&
        memcmp(post->remote_address, g_current_udp_req->remote_address, 28) == 0;
    int same_payload = same_event && post->payload_length == g_current_udp_req->payload_length &&
        memcmp(bytes, g_current_udp_payload, post->payload_length) == 0;
    if (same_event) {
        ++g_udp_post.count;
        if (g_udp_post.count == 1 && post->payload_length <= UDP_POST_PAYLOAD_MAX) {
            g_udp_post.valid = 1;
            g_udp_post.api = post->api;
            g_udp_post.status = post->status;
            memcpy(g_udp_post.remote_address, post->remote_address, 28);
            g_udp_post.option_flags = post->option_flags;
            g_udp_post.options_length = post->options_length;
            g_udp_post.payload_length = post->payload_length;
            memcpy(g_udp_post.payload, bytes, post->payload_length);
        } else {
            g_udp_post.valid = 0;
        }
    }
    const char *name = post->api == EXL_UDP_API_POST_SEND ? "nf_udpPostSend" :
                       post->api == EXL_UDP_API_POST_RECEIVE ? "nf_udpPostReceive" : "unknown-post";
    log_line("    %s event_tag=%llu id=0x%llx status=%d remote=%s len=%u hash=%016llx head=%s "
             "same_event=%d same_remote=%d same_payload=%d options={flags=0x%x,len=%u}",
             name, post->event_tag, post->endpoint_id, post->status, remote,
             post->payload_length, payload_hash(bytes, post->payload_length), head,
             same_event, same_remote, same_payload, post->option_flags, post->options_length);
}

static void handle_udp_state(const unsigned char *payload, unsigned int length)
{
    if (length < sizeof(struct exl_udp_state)) { log_line("udp state: malformed short frame"); return; }
    const struct exl_udp_state *state = (const struct exl_udp_state *)payload;
    unsigned int available = length - (unsigned int)sizeof(*state);
    if (state->data_length > available) { log_line("udp state: malformed data length"); return; }
    const unsigned char *data = payload + sizeof(*state);
    struct udp_endpoint *endpoint = udp_endpoint_by_id(state->endpoint_id);
    const char *name = state->api == EXL_UDP_API_ADD_REMOTE_RULE ? "nf_udpAddRemoteAddrRule" :
                       state->api == EXL_UDP_API_DISABLE_FILTERING ? "nf_udpDisableFiltering" :
                       state->api == EXL_UDP_API_ENABLE_FILTERING ? "nf_udpEnableFiltering" :
                       state->api == EXL_UDP_API_SET_FLOWCTL ? "nf_setUDPFlowCtl" : "unknown-state";
    if (state->api == EXL_UDP_API_ADD_REMOTE_RULE && state->data_length >= 20) {
        if (endpoint) ++endpoint->remote_rule_count;
        unsigned int host_address;
        unsigned short port_net, flags;
        memcpy(&host_address, data, 4); memcpy(&port_net, data + 16, 2); memcpy(&flags, data + 18, 2);
        struct in_addr address = { htonl(host_address) };
        int rule_added = 0;
        if (endpoint && flags == 1) {
            unsigned char remote_rule[28] = { 0 };
            remote_rule[0] = 2;
            memcpy(remote_rule + 2, &port_net, 2);
            memcpy(remote_rule + 4, &address, 4);
            if (!udp_endpoint_has_remote(endpoint, remote_rule) &&
                endpoint->remote_rule_unique_count < UDP_REMOTE_RULE_MAX) {
                memcpy(endpoint->remote_rules[endpoint->remote_rule_unique_count++], remote_rule, 28);
                rule_added = 1;
            }
        }
        char ip[INET_ADDRSTRLEN] = "?", raw[48];
        inet_ntop(AF_INET, &address, ip, sizeof(ip)); hex_head(raw, sizeof(raw), data, 20);
        log_line("    %s event_tag=%llu id=0x%llx status=%d remote=%s:%u flags=0x%x raw20=%s state=%s unique=%u",
                 name, state->event_tag, state->endpoint_id, state->status, ip,
                 (unsigned)ntohs(port_net), (unsigned)flags, raw,
                 rule_added ? "added" : "existing-or-unusable",
                 endpoint ? endpoint->remote_rule_unique_count : 0);
    } else {
        if (endpoint && state->api == EXL_UDP_API_SET_FLOWCTL)
            endpoint->flowctl_handle = state->value;
        log_line("    %s event_tag=%llu id=0x%llx status=%d value=%u data_len=%u",
                 name, state->event_tag, state->endpoint_id, state->status,
                 state->value, state->data_length);
    }
}

static int handle_nested_message(const struct exl_header *head, const unsigned char *payload)
{
    if (head->type == EXL_MSG_RESOLVE_PID_REQ) { serve_resolve_pid(payload, head->length); return 1; }
    if (head->type == EXL_MSG_UDP_POST) { handle_udp_post(payload, head->length); return 1; }
    if (head->type == EXL_MSG_UDP_STATE) { handle_udp_state(payload, head->length); return 1; }
    if (head->type == EXL_MSG_PONG && head->length == 0) return 1;
    return 0;
}

/* Ask the shim to run ExitLag's tcpConnectRequest. Returns 0 on a usable reply. */
static int ask_shim_tcp_connect(struct exl_tcp_connect_req *req, struct exl_tcp_connect_reply *reply)
{
    if (g_shim_fd < 0) return -1;
    if (send_frame(g_shim_fd, EXL_MSG_TCP_CONNECT_REQ, req, sizeof(*req)) != 0) {
        drop_shim("write failed");
        return -1;
    }
    for (;;) {
        struct exl_header head;
        if (read_exact(g_shim_fd, &head, sizeof(head), g_reply_timeout_ms) != 0) {
            log_line("no reply from shim within %d ms; failing open", g_reply_timeout_ms);
            return -1;
        }
        if (head.magic != EXL_MAGIC || head.length > EXL_MAX_PAYLOAD) { drop_shim("bad framing"); return -1; }
        static unsigned char buf[EXL_MAX_PAYLOAD];
        if (head.length && read_exact(g_shim_fd, buf, head.length, g_reply_timeout_ms) != 0) {
            drop_shim("short payload");
            return -1;
        }
        if (head.type == EXL_MSG_HELLO && head.length >= sizeof(struct exl_hello)) {
            const struct exl_hello *hello = (const struct exl_hello *)buf;
            g_shim_windows_pid = hello->shim_process_id;
            struct exl_hello_ack ack = { EXL_PROTOCOL_VERSION, (unsigned int)getpid(), 0 };
            send_frame(g_shim_fd, EXL_MSG_HELLO_ACK, &ack, sizeof(ack));
            continue;
        }
        if (handle_nested_message(&head, buf)) continue;
        if (head.type == EXL_MSG_TCP_CONNECT_REPLY && head.length >= sizeof(*reply)) {
            memcpy(reply, buf, sizeof(*reply));
            if (reply->tag != req->tag) continue;   /* stale reply, keep reading */
            return 0;
        }
    }
}

static int wait_for_shim_reply(unsigned short expected_type, unsigned long long tag,
                               void *reply, unsigned int reply_length)
{
    for (;;) {
        struct exl_header head;
        if (read_exact(g_shim_fd, &head, sizeof(head), g_reply_timeout_ms) != 0) {
            log_line("no UDP reply from shim within %d ms; failing open", g_reply_timeout_ms);
            return -1;
        }
        if (head.magic != EXL_MAGIC || head.length > EXL_MAX_PAYLOAD) { drop_shim("bad framing"); return -1; }
        static unsigned char buf[EXL_MAX_PAYLOAD];
        if (head.length && read_exact(g_shim_fd, buf, head.length, g_reply_timeout_ms) != 0) {
            drop_shim("short payload"); return -1;
        }
        if (handle_nested_message(&head, buf)) continue;
        if (head.type == expected_type && head.length >= reply_length) {
            unsigned long long reply_tag;
            memcpy(&reply_tag, buf, sizeof(reply_tag));
            if (reply_tag != tag) continue;
            memcpy(reply, buf, reply_length);
            return 0;
        }
    }
}

static int ask_shim_udp_lifecycle(struct exl_udp_lifecycle_req *req, int created,
                                  struct exl_udp_lifecycle_reply *reply)
{
    unsigned short request_type = created ? EXL_MSG_UDP_CREATED_REQ : EXL_MSG_UDP_CLOSED_REQ;
    unsigned short reply_type = created ? EXL_MSG_UDP_CREATED_REPLY : EXL_MSG_UDP_CLOSED_REPLY;
    if (g_shim_fd < 0 || send_frame(g_shim_fd, request_type, req, sizeof(*req)) != 0) return -1;
    return wait_for_shim_reply(reply_type, req->tag, reply, sizeof(*reply));
}

static int ask_shim_udp_connect(struct exl_udp_connect_req *req,
                                struct exl_udp_connect_reply *reply)
{
    if (g_shim_fd < 0 || send_frame(g_shim_fd, EXL_MSG_UDP_CONNECT_REQ, req, sizeof(*req)) != 0) return -1;
    return wait_for_shim_reply(EXL_MSG_UDP_CONNECT_REPLY, req->tag, reply, sizeof(*reply));
}

static int ask_shim_udp_datagram(unsigned char *frame, unsigned int frame_length,
                                 struct exl_udp_datagram_reply *reply)
{
    struct exl_udp_datagram_req *req = (struct exl_udp_datagram_req *)frame;
    if (g_shim_fd < 0 || send_frame(g_shim_fd, EXL_MSG_UDP_DATAGRAM_REQ, frame, frame_length) != 0) return -1;
    g_current_udp_req = req;
    g_current_udp_payload = frame + sizeof(*req) + req->options_length;
    memset(&g_udp_post, 0, sizeof(g_udp_post));
    int result = wait_for_shim_reply(EXL_MSG_UDP_DATAGRAM_REPLY, req->tag, reply, sizeof(*reply));
    g_current_udp_req = NULL; g_current_udp_payload = NULL;
    return result;
}

static void accept_shim(void)
{
    int fd = accept(g_listen_fd, NULL, NULL);
    if (fd < 0) return;

    /* Authenticate the peer before it can influence anything. Loopback alone is
       not a trust boundary: every local account can reach this port, and this
       process applies the destination rewrites its peer asks for.

       SO_PEERCRED is not available here -- it is an AF_UNIX facility and Wine's
       ws2_32 has no AF_UNIX, which is why this socket is TCP in the first
       place. The connecting socket's owning uid is instead read from the
       kernel's own accounting in /proc/net/tcp. */
    uid_t peer_uid;
    if (peer_uid_of_connection(fd, &peer_uid) != 0) {
        log_line("REFUSED shim connection: could not determine the peer's uid");
        close(fd);
        return;
    }
    if (peer_uid != 0 && peer_uid != g_allowed_peer_uid) {
        log_line("REFUSED shim connection from uid=%u (allowed: 0 or %u). "
                 "A process outside the owning account tried to drive the backend.",
                 (unsigned)peer_uid, (unsigned)g_allowed_peer_uid);
        close(fd);
        return;
    }

    if (g_shim_fd >= 0) {
        log_line("replacing existing shim connection (new peer uid=%u)", (unsigned)peer_uid);
        close(g_shim_fd);
    }
    log_line("shim peer uid=%u accepted", (unsigned)peer_uid);
    g_shim_fd = fd;
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    log_line("shim connected");

    /*
     * The handshake must complete, and prove knowledge of this session's token,
     * before anything else is served. A peer that fails here is disconnected
     * without ever reaching a routing code path.
     */
    struct exl_header head;
    if (read_exact(fd, &head, sizeof(head), 2000) != 0 || head.magic != EXL_MAGIC ||
        head.type != EXL_MSG_HELLO || head.length > EXL_MAX_PAYLOAD) {
        log_line("REFUSED shim connection: no valid HELLO");
        drop_shim("handshake missing or malformed");
        return;
    }
    struct exl_hello hello;
    memset(&hello, 0, sizeof(hello));
    if (head.length < sizeof(hello)) {
        log_line("REFUSED shim connection: HELLO is %u bytes, expected %zu "
                 "(an unauthenticated or outdated client)",
                 head.length, sizeof(hello));
        drop_shim("short handshake");
        return;
    }
    if (read_exact(fd, &hello, sizeof(hello), 2000) != 0) {
        log_line("REFUSED shim connection: HELLO body not received");
        drop_shim("handshake truncated");
        return;
    }
    /* Drain any bytes beyond the struct so framing stays aligned. */
    for (unsigned int extra = head.length - (unsigned int)sizeof(hello); extra; ) {
        unsigned char scratch[256];
        unsigned int chunk = extra > sizeof(scratch) ? (unsigned int)sizeof(scratch) : extra;
        if (read_exact(fd, scratch, chunk, 2000) != 0) { drop_shim("handshake truncated"); return; }
        extra -= chunk;
    }

    if (hello.version != EXL_PROTOCOL_VERSION) {
        log_line("hello: shim protocol=%u (expected %u)", hello.version, EXL_PROTOCOL_VERSION);
        drop_shim("protocol version mismatch");
        return;
    }
    if (!token_equal(hello.token, g_session_token)) {
        /* Never log either token, not even a prefix. */
        log_line("REFUSED shim connection from uid=%u: session token mismatch. "
                 "Only the process given this session's token may drive the backend.",
                 (unsigned)peer_uid);
        drop_shim("bad session token");
        return;
    }

    g_shim_windows_pid = hello.shim_process_id;
    struct exl_hello_ack ack = { EXL_PROTOCOL_VERSION, (unsigned int)getpid(), 0 };
    send_frame(fd, EXL_MSG_HELLO_ACK, &ack, sizeof(ack));
    g_shim_authenticated = 1;
    log_line("hello: shim protocol=%u exitlag_windows_pid=%u (token accepted)",
             hello.version, hello.shim_process_id);
}

/* --------------------------------------------------------------- nfqueue */

static struct mnl_socket *g_nl;
static unsigned int g_portid;

static void put_sockaddr(unsigned char *out, int family, const void *addr, unsigned short port_net)
{
    memset(out, 0, 28);
    if (family == AF_INET) {
        out[0] = 2; out[1] = 0;
        memcpy(out + 2, &port_net, 2);
        memcpy(out + 4, addr, 4);
    } else {
        out[0] = 23; out[1] = 0;
        memcpy(out + 2, &port_net, 2);
        memcpy(out + 8, addr, 16);
    }
}

static void format_endpoint(char *buf, size_t len, int family, const void *addr, unsigned short port_net)
{
    char ip[INET6_ADDRSTRLEN] = "?";
    inet_ntop(family, addr, ip, sizeof(ip));
    snprintf(buf, len, "%s%s%s:%u", family == AF_INET ? "" : "[", ip,
             family == AF_INET ? "" : "]", ntohs(port_net));
}

static int udp_emit_lifecycle(struct udp_endpoint *endpoint, int created)
{
    struct exl_udp_lifecycle_req req;
    memset(&req, 0, sizeof(req));
    req.tag = g_next_tag++; req.endpoint_id = endpoint->id;
    req.process_id = (unsigned int)endpoint->linux_pid;
    remember_observed_pid(req.process_id);
    req.ip_family = (unsigned short)(endpoint->family == AF_INET ? 2 : 23);
    memcpy(req.local_address, endpoint->local_address, 28);
    struct exl_udp_lifecycle_reply reply;
    memset(&reply, 0, sizeof(reply));
    if (ask_shim_udp_lifecycle(&req, created, &reply) != 0) {
        log_line("udp %s tag=%llu id=0x%llx pid=%d exe=%s | no shim reply; packet remains unchanged",
                 created ? "created" : "closed", req.tag, endpoint->id, endpoint->linux_pid,
                 endpoint->exe[0] ? endpoint->exe : "?");
        return -1;
    }
    endpoint->effective_pid = reply.process_id;
    char local[96]; wire_endpoint(local, sizeof(local), endpoint->local_address);
    log_line("udp %s tag=%llu id=0x%llx local=%s linux_pid=%d exe=%s effective_pid=%u "
             "verdict=%u mutated_info=%u remote_rules=%u flowctl=%u",
             created ? "udpCreated" : "udpClosed", req.tag, endpoint->id, local,
             endpoint->linux_pid, endpoint->exe[0] ? endpoint->exe : "?",
             reply.process_id, reply.verdict, reply.mutated_info,
             endpoint->remote_rule_count, endpoint->flowctl_handle);
    return 0;
}

static struct udp_endpoint *udp_endpoint_get(int family, const void *local_ip,
                                              unsigned short local_port_net)
{
    unsigned char local_address[28];
    put_sockaddr(local_address, family, local_ip, local_port_net);
    for (unsigned int i = 0; i < UDP_ENDPOINT_MAX; ++i) {
        struct udp_endpoint *endpoint = &g_udp_endpoints[i];
        if (endpoint->used && endpoint->family == family &&
            memcmp(endpoint->local_address, local_address, 28) == 0) {
            clock_gettime(CLOCK_MONOTONIC, &endpoint->last_seen);
            if (!endpoint->created_sent && udp_emit_lifecycle(endpoint, 1) == 0)
                endpoint->created_sent = 1;
            return endpoint;
        }
    }

    unsigned long inode = find_udp_socket_inode(family, local_ip, ntohs(local_port_net));
    if (inode) {
        for (unsigned int i = 0; i < UDP_ENDPOINT_MAX; ++i) {
            struct udp_endpoint *endpoint = &g_udp_endpoints[i];
            if (endpoint->used && endpoint->inode == inode) {
                clock_gettime(CLOCK_MONOTONIC, &endpoint->last_seen);
                return endpoint;
            }
        }
    }

    struct udp_endpoint *endpoint = NULL;
    for (unsigned int i = 0; i < UDP_ENDPOINT_MAX; ++i)
        if (!g_udp_endpoints[i].used) { endpoint = &g_udp_endpoints[i]; break; }
    if (!endpoint) { log_line("udp endpoint table full; accepting packet unchanged"); return NULL; }

    memset(endpoint, 0, sizeof(*endpoint));
    endpoint->used = 1; endpoint->family = family; endpoint->id = g_next_endpoint++;
    endpoint->inode = inode; endpoint->filtering_flag = 0x12;
    memcpy(endpoint->local_address, local_address, 28);
    clock_gettime(CLOCK_MONOTONIC, &endpoint->last_seen);
    if (inode) endpoint->linux_pid = pid_for_inode(inode, endpoint->exe, sizeof(endpoint->exe));
    if (endpoint->linux_pid) resolve_pid_path((unsigned int)endpoint->linux_pid,
                                              endpoint->exe, sizeof(endpoint->exe));
    if (udp_emit_lifecycle(endpoint, 1) == 0) endpoint->created_sent = 1;
    if (g_last_candidates[0]) log_line("    udp holders: %s", g_last_candidates);
    return endpoint;
}

static int udp_emit_connect(struct udp_endpoint *endpoint, const unsigned char remote_address[28])
{
    struct exl_udp_connect_req req;
    memset(&req, 0, sizeof(req));
    req.tag = g_next_tag++; req.endpoint_id = endpoint->id;
    req.process_id = (unsigned int)endpoint->linux_pid;
    remember_observed_pid(req.process_id);
    req.filtering_flag = endpoint->filtering_flag;
    req.ip_family = (unsigned short)(endpoint->family == AF_INET ? 2 : 23);
    memcpy(req.local_address, endpoint->local_address, 28);
    memcpy(req.remote_address, remote_address, 28);
    struct exl_udp_connect_reply reply;
    memset(&reply, 0, sizeof(reply));
    if (ask_shim_udp_connect(&req, &reply) != 0) return -1;
    endpoint->effective_pid = reply.process_id;
    endpoint->filtering_flag = reply.filtering_flag;
    memcpy(endpoint->first_remote, remote_address, 28);
    char local[96], remote[96], after[96];
    wire_endpoint(local, sizeof(local), endpoint->local_address);
    wire_endpoint(remote, sizeof(remote), remote_address);
    wire_endpoint(after, sizeof(after), reply.remote_address);
    log_line("udp udpConnectRequest tag=%llu id=0x%llx linux=%s -> %s linux_pid=%d exe=%s "
             "effective_pid=%u flag=0x%x->0x%x remote_after=%s verdict=%u mutated={flag=%u,remote=%u} | OBSERVE unchanged",
             req.tag, endpoint->id, local, remote, endpoint->linux_pid,
             endpoint->exe[0] ? endpoint->exe : "?", reply.process_id,
             req.filtering_flag, reply.filtering_flag, after, reply.verdict,
             reply.mutated_flag, reply.mutated_remote);
    return 0;
}

static void udp_reap_endpoints(void)
{
    static time_t last_reap;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (now.tv_sec == last_reap) return;
    last_reap = now.tv_sec;
    for (unsigned int i = 0; i < UDP_ENDPOINT_MAX; ++i) {
        struct udp_endpoint *endpoint = &g_udp_endpoints[i];
        if (!endpoint->used) continue;
        long long idle = now.tv_sec - endpoint->last_seen.tv_sec;
        int closed = endpoint->inode ? (idle >= 2 && !udp_inode_present(endpoint->family, endpoint->inode))
                                     : idle >= 60;
        if (!closed) continue;
        if (endpoint->created_sent) udp_emit_lifecycle(endpoint, 0);
        log_line("udp endpoint release id=0x%llx reason=%s datagrams={out=%llu,in=%llu} remote_rules_deleted=%u",
                 endpoint->id, endpoint->inode ? "socket-closed" : "idle-timeout",
                 endpoint->datagrams_out, endpoint->datagrams_in, endpoint->remote_rule_count);
        memset(endpoint, 0, sizeof(*endpoint));
    }
}

static void send_verdict(int queue_num, unsigned int packet_id, unsigned int verdict)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh = mnl_nlmsg_put_header(buf);
    nlh->nlmsg_type = (NFNL_SUBSYS_QUEUE << 8) | NFQNL_MSG_VERDICT;
    nlh->nlmsg_flags = NLM_F_REQUEST;
    struct nfgenmsg *nfg = mnl_nlmsg_put_extra_header(nlh, sizeof(*nfg));
    nfg->nfgen_family = AF_UNSPEC;
    nfg->version = NFNETLINK_V0;
    nfg->res_id = htons((uint16_t)queue_num);
    struct nfqnl_msg_verdict_hdr vh = { htonl(verdict), htonl(packet_id) };
    mnl_attr_put(nlh, NFQA_VERDICT_HDR, sizeof(vh), &vh);
    if (mnl_socket_sendto(g_nl, nlh, nlh->nlmsg_len) < 0)
        log_line("verdict send failed: %s", strerror(errno));
}

static uint32_t checksum_add(uint32_t sum, const unsigned char *data, size_t length)
{
    while (length >= 2) {
        sum += ((uint32_t)data[0] << 8) | data[1];
        data += 2; length -= 2;
    }
    if (length) sum += (uint32_t)data[0] << 8;
    return sum;
}

static unsigned short checksum_finish(uint32_t sum)
{
    while (sum >> 16) sum = (sum & 0xffffu) + (sum >> 16);
    return htons((unsigned short)~sum);
}

/* Execute the only live-proven outbound post form: one unchanged IPv4 UDP
   payload/address, emitted from the original socket tuple. The skb mark keeps
   the replacement packet out of the NFQUEUE rule. The caller drops the queued
   original only after sendto() has accepted the complete replacement. */
static int udp_reinject_post_send(const struct exl_udp_datagram_req *req,
                                  const struct udp_post_capture *post)
{
    if (req->ip_family != 2 || req->local_address[0] != 2 ||
        post->remote_address[0] != 2 || post->payload_length > UDP_POST_PAYLOAD_MAX)
        return -1;

    static int raw_fd = -1;
    if (raw_fd < 0) {
        raw_fd = socket(AF_INET, SOCK_RAW, IPPROTO_RAW);
        if (raw_fd < 0) { log_line("UDP reinject: raw socket failed: %s", strerror(errno)); return -1; }
        int one = 1;
        unsigned int mark = UDP_REINJECT_MARK;
        if (setsockopt(raw_fd, IPPROTO_IP, IP_HDRINCL, &one, sizeof(one)) < 0 ||
            setsockopt(raw_fd, SOL_SOCKET, SO_MARK, &mark, sizeof(mark)) < 0) {
            log_line("UDP reinject: socket setup failed: %s", strerror(errno));
            close(raw_fd); raw_fd = -1; return -1;
        }
    }

    size_t total = sizeof(struct iphdr) + sizeof(struct udphdr) + post->payload_length;
    unsigned char *packet = calloc(1, total);
    if (!packet) return -1;
    struct iphdr *ip = (struct iphdr *)packet;
    struct udphdr *udp = (struct udphdr *)(packet + sizeof(*ip));
    unsigned char *bytes = packet + sizeof(*ip) + sizeof(*udp);

    ip->version = 4; ip->ihl = 5; ip->ttl = 64; ip->protocol = IPPROTO_UDP;
    ip->tot_len = htons((unsigned short)total);
    memcpy(&ip->saddr, req->local_address + 4, 4);
    memcpy(&ip->daddr, post->remote_address + 4, 4);
    memcpy(&udp->source, req->local_address + 2, 2);
    memcpy(&udp->dest, post->remote_address + 2, 2);
    udp->len = htons((unsigned short)(sizeof(*udp) + post->payload_length));
    memcpy(bytes, post->payload, post->payload_length);

    unsigned char pseudo_tail[4] = { 0, IPPROTO_UDP,
        (unsigned char)(ntohs(udp->len) >> 8), (unsigned char)ntohs(udp->len) };
    uint32_t sum = 0;
    sum = checksum_add(sum, (const unsigned char *)&ip->saddr, 4);
    sum = checksum_add(sum, (const unsigned char *)&ip->daddr, 4);
    sum = checksum_add(sum, pseudo_tail, sizeof(pseudo_tail));
    sum = checksum_add(sum, (const unsigned char *)udp, sizeof(*udp));
    sum = checksum_add(sum, bytes, post->payload_length);
    udp->check = checksum_finish(sum);
    if (!udp->check) udp->check = 0xffff;
    ip->check = checksum_finish(checksum_add(0, packet, sizeof(*ip)));

    struct sockaddr_in destination = { 0 };
    destination.sin_family = AF_INET;
    destination.sin_port = udp->dest;
    destination.sin_addr.s_addr = ip->daddr;
    ssize_t sent = sendto(raw_fd, packet, total, MSG_DONTWAIT,
                          (struct sockaddr *)&destination, sizeof(destination));
    int saved_errno = errno;
    free(packet);
    if (sent != (ssize_t)total) {
        errno = saved_errno;
        log_line("UDP reinject: sendto wrote %zd/%zu: %s", sent, total,
                 sent < 0 ? strerror(errno) : "short write");
        return -1;
    }
    return 0;
}

static void observe_udp_packet(int queue_num, unsigned int packet_id, int family,
                               const void *src, const void *dst,
                               const unsigned char *l4, unsigned int l4_len,
                               unsigned int hook)
{
    if (l4_len < sizeof(struct udphdr)) { send_verdict(queue_num, packet_id, NF_ACCEPT); return; }
    const struct udphdr *udp = (const struct udphdr *)l4;
    unsigned int wire_length = ntohs(udp->len);
    if (wire_length < sizeof(*udp)) { send_verdict(queue_num, packet_id, NF_ACCEPT); return; }
    unsigned int payload_length = wire_length - (unsigned int)sizeof(*udp);
    if (payload_length > l4_len - sizeof(*udp)) payload_length = l4_len - (unsigned int)sizeof(*udp);
    const unsigned char *datagram = l4 + sizeof(*udp);

    int direction;
    const void *local_ip, *remote_ip;
    unsigned short local_port, remote_port;
    if (hook == NF_INET_LOCAL_OUT) {
        direction = EXL_DIR_OUT; local_ip = src; remote_ip = dst;
        local_port = udp->source; remote_port = udp->dest;
    } else if (hook == NF_INET_LOCAL_IN) {
        direction = EXL_DIR_IN; local_ip = dst; remote_ip = src;
        local_port = udp->dest; remote_port = udp->source;
    } else {
        send_verdict(queue_num, packet_id, NF_ACCEPT); return;
    }

    struct udp_endpoint *endpoint = udp_endpoint_get(family, local_ip, local_port);
    if (!endpoint || !endpoint->created_sent) {
        send_verdict(queue_num, packet_id, NF_ACCEPT); return;
    }
    if (g_udp_exe && (!endpoint->exe[0] || !strcasestr(endpoint->exe, g_udp_exe))) {
        log_line("udp process gate: id=0x%llx pid=%d exe=%s does not match %s; ACCEPT unchanged",
                 endpoint->id, endpoint->linux_pid, endpoint->exe[0] ? endpoint->exe : "?", g_udp_exe);
        send_verdict(queue_num, packet_id, NF_ACCEPT); return;
    }
    if (g_udp_active_once && endpoint->active_ordinal == 0) {
        endpoint->active_ordinal = ++g_udp_eligible_endpoint_count;
        log_line("UDP eligible endpoint index=%u id=0x%llx", endpoint->active_ordinal,
                 endpoint->id);
    }
    unsigned char remote_address[28];
    put_sockaddr(remote_address, family, remote_ip, remote_port);
    if (direction == EXL_DIR_OUT && !endpoint->connect_sent) {
        if (udp_emit_connect(endpoint, remote_address) != 0) {
            send_verdict(queue_num, packet_id, NF_ACCEPT); return;
        }
        endpoint->connect_sent = 1;
    }

    static unsigned char frame[EXL_MAX_PAYLOAD];
    unsigned int frame_length = (unsigned int)sizeof(struct exl_udp_datagram_req) + payload_length;
    if (frame_length > sizeof(frame)) { send_verdict(queue_num, packet_id, NF_ACCEPT); return; }
    struct exl_udp_datagram_req *req = (struct exl_udp_datagram_req *)frame;
    memset(req, 0, sizeof(*req));
    req->tag = g_next_tag++; req->endpoint_id = endpoint->id;
    req->process_id = (unsigned int)endpoint->linux_pid; req->direction = (unsigned char)direction;
    remember_observed_pid(req->process_id);
    req->ip_family = (unsigned short)(family == AF_INET ? 2 : 23);
    memcpy(req->local_address, endpoint->local_address, 28);
    memcpy(req->remote_address, remote_address, 28);
    req->payload_length = payload_length;
    memcpy(frame + sizeof(*req), datagram, payload_length);

    if (direction == EXL_DIR_OUT) ++endpoint->datagrams_out;
    else ++endpoint->datagrams_in;
    struct exl_udp_datagram_reply reply;
    memset(&reply, 0, sizeof(reply));
    int have_reply = ask_shim_udp_datagram(frame, frame_length, &reply) == 0;
    char local[96], remote[96], head[72];
    wire_endpoint(local, sizeof(local), endpoint->local_address);
    wire_endpoint(remote, sizeof(remote), remote_address);
    hex_head(head, sizeof(head), datagram, payload_length);
    unsigned int packet_verdict = NF_ACCEPT;
    const char *decision = "ACCEPT unchanged (UDP observe)";
    if (g_apply_udp && have_reply) {
        int same_remote = memcmp(g_udp_post.remote_address, remote_address, 28) == 0;
        int same_payload = g_udp_post.payload_length == payload_length &&
                           memcmp(g_udp_post.payload, datagram, payload_length) == 0;
        int common = reply.verdict == EXL_VERDICT_UNCHANGED && reply.post_count == 1 &&
                     reply.control_count == 0 && !reply.mutated_remote &&
                     !reply.mutated_payload && !reply.mutated_options &&
                     g_udp_post.count == 1 && g_udp_post.valid && g_udp_post.status == 0 &&
                     g_udp_post.option_flags == 0 && g_udp_post.options_length == 0 &&
                     same_remote && same_payload && udp_endpoint_has_remote(endpoint, remote_address);
        int selected = !g_udp_active_once ||
                       (g_udp_active_endpoint != 0 &&
                        g_udp_active_endpoint == endpoint->id) ||
                       (g_udp_active_endpoint == 0 &&
                        endpoint->active_ordinal == g_udp_active_index);
        if (!selected) {
            decision = g_udp_active_endpoint == 0
                ? "ACCEPT unchanged (waiting for configured active endpoint index)"
                : "ACCEPT unchanged (different endpoint; active-once locked)";
        } else if (direction == EXL_DIR_OUT && common &&
            g_udp_post.api == EXL_UDP_API_POST_SEND && reply.remote_rule_count == 1) {
            if (udp_reinject_post_send(req, &g_udp_post) == 0) {
                if (g_udp_active_once && g_udp_active_endpoint == 0) {
                    g_udp_active_endpoint = endpoint->id;
                    log_line("UDP active-once selected index=%u id=0x%llx remote=%s",
                             endpoint->active_ordinal, endpoint->id, remote);
                }
                packet_verdict = NF_DROP;
                decision = "DROP original; POST_SEND reinjected with bypass mark";
            } else {
                decision = "ACCEPT unchanged (UDP reinject failed open)";
            }
        } else if (direction == EXL_DIR_IN && common &&
                   g_udp_post.api == EXL_UDP_API_POST_RECEIVE && reply.remote_rule_count == 0) {
            decision = "ACCEPT POST_RECEIVE reply to original socket";
        } else {
            decision = "ACCEPT unchanged (UDP contract outside proven active subset)";
        }
    }
    if (have_reply) {
        log_line("udp %s tag=%llu id=0x%llx packet=%llu linux=%s -> %s linux_pid=%d exe=%s "
                 "effective_pid=%u len=%u hash=%016llx head=%s callback={posts=%u,rules=%u,controls=%u,"
                 "direct_remote=%u,direct_payload=%u,direct_options=%u} flowctl=%u | %s",
                 direction == EXL_DIR_OUT ? "udpSend" : "udpReceive", req->tag, endpoint->id,
                 direction == EXL_DIR_OUT ? endpoint->datagrams_out : endpoint->datagrams_in,
                 local, remote, endpoint->linux_pid, endpoint->exe[0] ? endpoint->exe : "?",
                 endpoint->effective_pid, payload_length, payload_hash(datagram, payload_length), head,
                 reply.post_count, reply.remote_rule_count, reply.control_count,
                 reply.mutated_remote, reply.mutated_payload, reply.mutated_options,
                 endpoint->flowctl_handle, decision);
    } else {
        log_line("udp %s tag=%llu id=0x%llx linux=%s -> %s pid=%d exe=%s len=%u "
                 "hash=%016llx | no shim reply; ACCEPT unchanged (UDP fail-open)",
                 direction == EXL_DIR_OUT ? "udpSend" : "udpReceive", req->tag, endpoint->id,
                 local, remote, endpoint->linux_pid, endpoint->exe[0] ? endpoint->exe : "?",
                 payload_length, payload_hash(datagram, payload_length));
    }
    send_verdict(queue_num, packet_id, packet_verdict);
}

/*
 * Install one exact redirect decision in the map created by nft-setup.sh.
 *
 * The key contains the complete original IPv4 4-tuple, rather than merely the
 * remote endpoint: two simultaneous connections to the same game server may
 * receive different callback results and must never share policy. The element
 * expires shortly after this SYN. Once the NAT chain sees the SYN, conntrack
 * retains the translation for all later packets and no persistent map entry is
 * needed.
 *
 * /usr/bin/nft is invoked directly (never through a shell), and every token in
 * the command is produced by inet_ntop or an integer conversion. A failed map
 * update is a fail-open condition: the caller accepts the original SYN.
 */
/*
 * Install one exact 4-tuple DNAT element. The only difference between families
 * is the map name and the textual address form, so the same code path serves
 * both rather than a parallel IPv6 implementation.
 */
static int nft_arm_tcp_redirect(int family, const void *src, unsigned short sport_net,
                                const void *dst, unsigned short dport_net,
                                const void *new_dst, unsigned short new_dport_net)
{
    const char *map = (family == AF_INET6) ? "tcp_redirect_v6" : "tcp_redirect_v4";
    char src_text[INET6_ADDRSTRLEN], dst_text[INET6_ADDRSTRLEN], new_text[INET6_ADDRSTRLEN];
    if (!inet_ntop(family, src, src_text, sizeof(src_text)) ||
        !inet_ntop(family, dst, dst_text, sizeof(dst_text)) ||
        !inet_ntop(family, new_dst, new_text, sizeof(new_text)))
        return -1;

    char command[768];
    int length = snprintf(command, sizeof(command),
        "destroy element inet exitlag %s { %s . %u . %s . %u }\n"
        "add element inet exitlag %s { "
        "%s . %u . %s . %u timeout 10s : %s . %u }\n",
        map, src_text, (unsigned)ntohs(sport_net), dst_text, (unsigned)ntohs(dport_net),
        map, src_text, (unsigned)ntohs(sport_net), dst_text, (unsigned)ntohs(dport_net),
        new_text, (unsigned)ntohs(new_dport_net));
    if (length <= 0 || (size_t)length >= sizeof(command)) return -1;

    int input[2];
    if (pipe(input) != 0) return -1;
    pid_t child = fork();
    if (child < 0) { close(input[0]); close(input[1]); return -1; }
    if (child == 0) {
        if (dup2(input[0], STDIN_FILENO) < 0) _exit(126);
        close(input[0]); close(input[1]);
        execl("/usr/bin/nft", "nft", "-f", "-", (char *)NULL);
        _exit(127);
    }

    close(input[0]);
    size_t written = 0;
    while (written < (size_t)length) {
        ssize_t n = write(input[1], command + written, (size_t)length - written);
        if (n < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (n == 0) break;
        written += (size_t)n;
    }
    close(input[1]);

    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) return -1;
    }
    return written == (size_t)length && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

static int parse_attrs(const struct nlattr *attr, void *data)
{
    const struct nlattr **tb = data;
    int type = mnl_attr_get_type(attr);
    if (mnl_attr_type_valid(attr, NFQA_MAX) < 0) return MNL_CB_OK;
    tb[type] = attr;
    return MNL_CB_OK;
}

static int on_packet(const struct nlmsghdr *nlh, void *data)
{
    (void)data;
    const struct nlattr *tb[NFQA_MAX + 1] = { 0 };
    mnl_attr_parse(nlh, sizeof(struct nfgenmsg), parse_attrs, tb);
    if (!tb[NFQA_PACKET_HDR]) return MNL_CB_OK;

    struct nfqnl_msg_packet_hdr *ph = mnl_attr_get_payload(tb[NFQA_PACKET_HDR]);
    unsigned int packet_id = ntohl(ph->packet_id);
    const struct nfgenmsg *nfg = mnl_nlmsg_get_payload(nlh);
    int queue_num = (int)ntohs(nfg->res_id);

    /* Every rejection or malformed input below deliberately fails open. */
    if (!tb[NFQA_PAYLOAD]) { send_verdict(queue_num, packet_id, NF_ACCEPT); return MNL_CB_OK; }

    unsigned char *payload = mnl_attr_get_payload(tb[NFQA_PAYLOAD]);
    unsigned int payload_len = mnl_attr_get_payload_len(tb[NFQA_PAYLOAD]);
    if (payload_len < 20) { send_verdict(queue_num, packet_id, NF_ACCEPT); return MNL_CB_OK; }

    int family; const void *src, *dst; const unsigned char *l4; unsigned int l4_len; unsigned char proto;
    if ((payload[0] >> 4) == 4) {
        struct iphdr *ip = (struct iphdr *)payload;
        unsigned int ihl = ip->ihl * 4u;
        if (ihl < 20u || payload_len < ihl) { send_verdict(queue_num, packet_id, NF_ACCEPT); return MNL_CB_OK; }
        family = AF_INET; src = &ip->saddr; dst = &ip->daddr; proto = ip->protocol;
        l4 = payload + ihl; l4_len = payload_len - ihl;
    } else if ((payload[0] >> 4) == 6) {
        struct ip6_hdr *ip6 = (struct ip6_hdr *)payload;
        if (payload_len < 40u) { send_verdict(queue_num, packet_id, NF_ACCEPT); return MNL_CB_OK; }
        family = AF_INET6; src = &ip6->ip6_src; dst = &ip6->ip6_dst;
        proto = ip6->ip6_ctlun.ip6_un1.ip6_un1_nxt;
        l4 = payload + 40; l4_len = payload_len - 40;
    } else { send_verdict(queue_num, packet_id, NF_ACCEPT); return MNL_CB_OK; }

    if (g_observe_udp && queue_num == g_udp_queue_num && proto == IPPROTO_UDP) {
        observe_udp_packet(queue_num, packet_id, family, src, dst, l4, l4_len, ph->hook);
        return MNL_CB_OK;
    }
    if (queue_num != g_queue_num) {
        send_verdict(queue_num, packet_id, NF_ACCEPT); return MNL_CB_OK;
    }

    if (proto != IPPROTO_TCP || l4_len < sizeof(struct tcphdr)) {
        send_verdict(queue_num, packet_id, NF_ACCEPT);
        return MNL_CB_OK;
    }
    const struct tcphdr *tcp = (const struct tcphdr *)l4;
    if (!tcp->syn || tcp->ack) {          /* only the first SYN starts a flow */
        send_verdict(queue_num, packet_id, NF_ACCEPT);
        return MNL_CB_OK;
    }

    unsigned char local_ip[16] = { 0 };
    memcpy(local_ip, src, family == AF_INET ? 4 : 16);

    /*
     * Wine allocates the socket inside wineserver and only then passes the fd to
     * the application, so immediately after connect() the sole holder can be
     * wineserver. Attributing the flow to wineserver would hide the real game
     * process from ExitLag, so retry briefly until a non-plumbing owner appears.
     * Bounded and cheap: the packet is already queued, and the loop gives up
     * quickly rather than delaying the verdict.
     */
    unsigned long inode = 0;
    char exe[256] = "";
    int linux_pid = 0;
    int wait_ms = 0;
    {
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        for (int attempt = 0; attempt < ATTR_MAX_ATTEMPTS; ++attempt) {
            if (!inode) inode = find_socket_inode(family, local_ip, ntohs(tcp->source));
            if (inode) {
                linux_pid = pid_for_inode(inode, exe, sizeof(exe));
                if (linux_pid && !pid_is_wine_plumbing_num(linux_pid)) break;
            }
            struct timespec nap = { 0, ATTR_STEP_MS * 1000 * 1000 };
            nanosleep(&nap, NULL);
        }
        clock_gettime(CLOCK_MONOTONIC, &t1);
        wait_ms = (int)((t1.tv_sec - t0.tv_sec) * 1000 + (t1.tv_nsec - t0.tv_nsec) / 1000000);
    }

    char from[96], to[96];
    format_endpoint(from, sizeof(from), family, src, tcp->source);
    format_endpoint(to, sizeof(to), family, dst, tcp->dest);

    struct exl_tcp_connect_req req;
    memset(&req, 0, sizeof(req));
    req.tag = g_next_tag++;
    req.endpoint_id = g_next_endpoint++;
    req.process_id = (unsigned int)linux_pid;   /* real owning PID; the shim resolves its path */
    remember_observed_pid(req.process_id);
    req.filtering_flag = 0x10;             /* NF_INDICATE_CONNECT_REQUESTS */
    req.direction = 2;                     /* NF_D_OUT */
    req.ip_family = (unsigned short)(family == AF_INET ? 2 : 23);
    put_sockaddr(req.local_address, family, src, tcp->source);
    put_sockaddr(req.remote_address, family, dst, tcp->dest);

    /* Flow ordering/timing, so port-dependence can be told apart from
       order- or state-dependence when the same destination is probed twice. */
    struct timespec now_ts;
    clock_gettime(CLOCK_MONOTONIC, &now_ts);
    long long since_ms = -1;
    if (g_last_flow.tv_sec)
        since_ms = (now_ts.tv_sec - g_last_flow.tv_sec) * 1000LL +
                   (now_ts.tv_nsec - g_last_flow.tv_nsec) / 1000000LL;
    g_last_flow = now_ts;
    unsigned long long flow_index = ++g_flow_index;

    struct exl_tcp_connect_reply reply;
    memset(&reply, 0, sizeof(reply));
    int have_reply = (ask_shim_tcp_connect(&req, &reply) == 0);

    int redirect_armed = 0;
    int redirect_requested = 0;
    const char *redirect_failure = NULL;
    if (have_reply) {
        char after[96] = "";
        if (reply.mutated_remote) {
            int fam = reply.remote_address[0] == 23 ? AF_INET6 : AF_INET;
            unsigned short port_net;
            memcpy(&port_net, reply.remote_address + 2, 2);
            format_endpoint(after, sizeof(after), fam,
                            reply.remote_address + (fam == AF_INET ? 4 : 8), port_net);
        }

        /* Only a byte-real, callback-declared endpoint change is eligible.
           Padding-only sockaddr changes, wrong families and port zero all pass
           through unchanged. The same checks apply to both families; only the
           sockaddr layout and the "invalid target" set differ. */
        if (g_apply_tcp && (family == AF_INET || family == AF_INET6) &&
            reply.verdict == EXL_VERDICT_MUTATED && reply.mutated_remote) {
            const int v6 = (family == AF_INET6);
            /* sockaddr_in: family,port,addr@4. sockaddr_in6: family,port,flowinfo,addr@8. */
            const unsigned int addr_offset = v6 ? 8u : 4u;
            const size_t addr_len = v6 ? 16u : 4u;
            const unsigned short want_family = v6 ? 23u : 2u;   /* Windows AF_INET6 = 23 */
            const unsigned char *new_addr = reply.remote_address + addr_offset;
            unsigned short reply_family = (unsigned short)
                (reply.remote_address[0] | ((unsigned short)reply.remote_address[1] << 8));
            unsigned short new_port_net;
            memcpy(&new_port_net, reply.remote_address + 2, sizeof(new_port_net));
            int endpoint_changed = new_port_net != tcp->dest ||
                memcmp(new_addr, dst, addr_len) != 0;

            int invalid_target, non_loopback;
            if (v6) {
                struct in6_addr a6;
                memcpy(&a6, new_addr, sizeof(a6));
                /* :: is unspecified; ff00::/8 is multicast. */
                invalid_target = IN6_IS_ADDR_UNSPECIFIED(&a6) || IN6_IS_ADDR_MULTICAST(&a6);
                /* IPv6 equivalent of the 127/8 policy: ExitLag's proxy is ::1.
                   An IPv4-mapped loopback (::ffff:127.0.0.0/8) counts too, since
                   that is the same local proxy reached over a dual-stack socket. */
                non_loopback = !IN6_IS_ADDR_LOOPBACK(&a6) &&
                               !(IN6_IS_ADDR_V4MAPPED(&a6) && new_addr[12] == 127);
            } else {
                struct in_addr a4;
                memcpy(&a4, new_addr, sizeof(a4));
                invalid_target = a4.s_addr == htonl(INADDR_ANY) ||
                                 a4.s_addr == htonl(INADDR_BROADCAST) ||
                                 IN_MULTICAST(ntohl(a4.s_addr));
                non_loopback = (ntohl(a4.s_addr) >> 24) != 127;
            }

            if (reply_family != want_family)
                redirect_failure = v6 ? "callback returned non-IPv6 target for an IPv6 flow"
                                      : "callback returned non-IPv4 target";
            else if (!new_port_net) redirect_failure = "callback returned port zero";
            else if (invalid_target) redirect_failure = "callback returned invalid unicast target";
            else if (!endpoint_changed) redirect_failure = "callback changed only sockaddr metadata";
            else if (!g_allow_remote_redirect && non_loopback)
                redirect_failure = "callback returned a non-loopback target "
                                   "(refused: the proven runtime only redirects to ExitLag's local proxy; "
                                   "--allow-remote-redirect overrides)";
            else {
                redirect_requested = 1;
                if (nft_arm_tcp_redirect(family, src, tcp->source, dst, tcp->dest,
                                         new_addr, new_port_net) == 0)
                    redirect_armed = 1;
                else
                    redirect_failure = "nft map update failed";
            }
        }

        log_line("flow=%llu dt_ms=%lld tag=%llu tcp syn %s -> %s dport=%u pid=%d exe=%s inode=%lu | "
                 "attr_wait=%dms verdict=%u flag=0x%x->0x%x ctrlflow=%s%s%s | %s",
                 flow_index, since_ms, (unsigned long long)req.tag, from, to,
                 (unsigned)ntohs(tcp->dest), linux_pid, exe[0] ? exe : "?", inode,
                 wait_ms, reply.verdict, req.filtering_flag, reply.filtering_flag,
                 ((reply.filtering_flag & 0x200) && !(req.filtering_flag & 0x200)) ? "added" : "no",
                 reply.mutated_remote ? " remote->" : "", reply.mutated_remote ? after : "",
                 redirect_armed ? "DNAT armed; ACCEPT (active)" :
                 (g_apply_tcp ? "ACCEPT unchanged (fail-open/no redirect)" : "ACCEPT unchanged (passive)"));
        if (redirect_failure)
            log_line("    redirect not armed: %s%s", redirect_failure,
                     redirect_requested ? "; original destination retained" : "");
        if (g_last_candidates[0]) log_line("    holders: %s", g_last_candidates);
    } else {
        log_line("flow=%llu dt_ms=%lld tcp syn %s -> %s dport=%u pid=%d exe=%s | no shim reply | ACCEPT unchanged (fail-open)",
                 flow_index, since_ms, from, to, (unsigned)ntohs(tcp->dest), linux_pid, exe[0] ? exe : "?");
    }

    send_verdict(queue_num, packet_id, NF_ACCEPT);
    return MNL_CB_OK;
}

static int nfq_config(int queue_num, unsigned short command, unsigned int range, unsigned char mode)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh = mnl_nlmsg_put_header(buf);
    nlh->nlmsg_type = (NFNL_SUBSYS_QUEUE << 8) | NFQNL_MSG_CONFIG;
    nlh->nlmsg_flags = NLM_F_REQUEST;
    struct nfgenmsg *nfg = mnl_nlmsg_put_extra_header(nlh, sizeof(*nfg));
    nfg->nfgen_family = AF_UNSPEC;
    nfg->version = NFNETLINK_V0;
    nfg->res_id = htons((uint16_t)queue_num);
    if (command) {
        struct nfqnl_msg_config_cmd cmd = { 0 };
        cmd.command = (uint8_t)command;
        cmd.pf = htons(AF_INET);
        mnl_attr_put(nlh, NFQA_CFG_CMD, sizeof(cmd), &cmd);
    }
    if (mode) {
        struct nfqnl_msg_config_params params = { 0 };
        params.copy_range = htonl(range);
        params.copy_mode = mode;
        mnl_attr_put(nlh, NFQA_CFG_PARAMS, sizeof(params), &params);
    }
    return mnl_socket_sendto(g_nl, nlh, nlh->nlmsg_len) < 0 ? -1 : 0;
}

static int nfq_set_fail_open(int queue_num)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh = mnl_nlmsg_put_header(buf);
    nlh->nlmsg_type = (NFNL_SUBSYS_QUEUE << 8) | NFQNL_MSG_CONFIG;
    nlh->nlmsg_flags = NLM_F_REQUEST;
    struct nfgenmsg *nfg = mnl_nlmsg_put_extra_header(nlh, sizeof(*nfg));
    nfg->nfgen_family = AF_UNSPEC; nfg->version = NFNETLINK_V0;
    nfg->res_id = htons((uint16_t)queue_num);
    mnl_attr_put_u32(nlh, NFQA_CFG_QUEUE_MAXLEN, htonl(4096));
#ifdef NFQA_CFG_F_FAIL_OPEN
    mnl_attr_put_u32(nlh, NFQA_CFG_FLAGS, htonl(NFQA_CFG_F_FAIL_OPEN));
    mnl_attr_put_u32(nlh, NFQA_CFG_MASK, htonl(NFQA_CFG_F_FAIL_OPEN));
#endif
    return mnl_socket_sendto(g_nl, nlh, nlh->nlmsg_len) < 0 ? -1 : 0;
}

static int nfq_bind_one(int queue_num)
{
    return nfq_config(queue_num, NFQNL_CFG_CMD_BIND, 0, 0) == 0 &&
           nfq_config(queue_num, 0, 0xffff, NFQNL_COPY_PACKET) == 0 &&
           nfq_set_fail_open(queue_num) == 0 ? 0 : -1;
}


/*
 * Bring-up aid: synthesize one flow event (no packet involved) so the
 * Linux -> shim -> ExitLag -> shim -> Linux round trip can be proven before
 * any netfilter rule exists.
 */
static void run_self_test(const char *dest)
{
    char host[64]; unsigned int port = 80;
    snprintf(host, sizeof(host), "%s", dest);
    char *colon = strrchr(host, ':');
    if (colon) { *colon = 0; port = (unsigned int)strtoul(colon + 1, NULL, 10); }
    struct in_addr addr;
    if (inet_pton(AF_INET, host, &addr) != 1) { log_line("self-test: bad destination '%s'", dest); return; }

    struct in_addr local;
    inet_pton(AF_INET, "127.0.0.1", &local);

    struct exl_tcp_connect_req req;
    memset(&req, 0, sizeof(req));
    req.tag = g_next_tag++;
    req.endpoint_id = g_next_endpoint++;
    req.process_id = 0;
    req.filtering_flag = 0x10;
    req.direction = 2;
    req.ip_family = 2;
    put_sockaddr(req.local_address, AF_INET, &local, htons(54321));
    put_sockaddr(req.remote_address, AF_INET, &addr, htons((unsigned short)port));

    struct exl_tcp_connect_reply reply;
    memset(&reply, 0, sizeof(reply));
    if (ask_shim_tcp_connect(&req, &reply) != 0) { log_line("self-test: no reply from shim"); return; }
    log_line("self-test: dest=%s:%u verdict=%u flag=0x%x->0x%x mutated_flag=%u mutated_remote=%u",
             host, port, reply.verdict, req.filtering_flag, reply.filtering_flag,
             reply.mutated_flag, reply.mutated_remote);
}

int main(int argc, char **argv)
{
    unsigned short port = EXL_DEFAULT_PORT;
    const char *logpath = NULL;
    const char *identity_map_path = NULL;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--queue") && i + 1 < argc) g_queue_num = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--port") && i + 1 < argc) port = (unsigned short)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--log") && i + 1 < argc) logpath = argv[++i];
        else if (!strcmp(argv[i], "--timeout-ms") && i + 1 < argc) g_reply_timeout_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--no-queue")) g_no_queue = 1;
        else if (!strcmp(argv[i], "--peer-uid") && i + 1 < argc) g_allowed_peer_uid = (uid_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--token-file") && i + 1 < argc) snprintf(g_token_path, sizeof(g_token_path), "%s", argv[++i]);
        else if (!strcmp(argv[i], "--allow-remote-redirect")) g_allow_remote_redirect = 1;
        else if (!strcmp(argv[i], "--allow-any-pid-resolve")) g_allow_any_pid_resolve = 1;
        else if (!strcmp(argv[i], "--apply-tcp")) g_apply_tcp = 1;
        else if (!strcmp(argv[i], "--apply-udp")) { g_apply_udp = 1; g_observe_udp = 1; }
        else if (!strcmp(argv[i], "--udp-active-once")) g_udp_active_once = 1;
        else if (!strcmp(argv[i], "--udp-active-index") && i + 1 < argc) {
            char *end = NULL;
            unsigned long value = strtoul(argv[++i], &end, 10);
            if (!argv[i][0] || !end || *end || value == 0 || value > UINT_MAX) {
                fprintf(stderr, "--udp-active-index must be a positive integer\n"); return 2;
            }
            g_udp_active_index = (unsigned int)value;
        }
        else if (!strcmp(argv[i], "--observe-udp")) g_observe_udp = 1;
        else if (!strcmp(argv[i], "--udp-queue") && i + 1 < argc) g_udp_queue_num = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--udp-exe") && i + 1 < argc) g_udp_exe = argv[++i];
        else if (!strcmp(argv[i], "--identity-map") && i + 1 < argc) identity_map_path = argv[++i];
        else if (!strcmp(argv[i], "--self-test") && i + 1 < argc) g_selftest = argv[++i];
        else { fprintf(stderr,
            "usage: %s [--queue N] [--port N] [--log FILE] [--timeout-ms N]\n"
            "          [--no-queue] [--apply-tcp] [--observe-udp|--apply-udp] [--udp-active-once]\n"
            "          [--udp-active-index N]\n"
            "          [--udp-queue N] [--udp-exe TEXT] [--identity-map FILE]\n"
            "          [--self-test IPV4:PORT]\n"
            "  --no-queue   do not touch netfilter at all (validates the shim link only)\n"
            "  --peer-uid   uid permitted to connect besides root (default: $SUDO_UID)\n"
            "  --token-file where to publish this session's IPC token (0600, owner-only)\n"
            "  --allow-remote-redirect  permit non-loopback DNAT targets (default: refuse)\n"
            "  --allow-any-pid-resolve  resolve PIDs this helper never reported (test harnesses only)\n"
            "  --apply-tcp  apply valid IPv4 address+port mutations via nftables DNAT\n"
            "  --observe-udp deliver UDP callbacks but always ACCEPT original packets\n"
            "  --apply-udp   execute only one live-proven post/rule form; all else ACCEPTs\n"
            "  --udp-active-once lock active UDP to one eligible endpoint id\n"
            "  --udp-active-index select the Nth eligible endpoint (1-based; default 1)\n"
            "  --udp-queue  NFQUEUE for both UDP directions (default: TCP queue + 1)\n"
            "  --udp-exe    accept queued UDP unchanged unless owner path contains TEXT\n"
            "  --identity-map exact SOURCE<TAB>WINDOWS_PATH translations; enables strict identity mode\n"
            "  --self-test  once the shim connects, synthesize one flow event to this\n"
            "               destination and report what ExitLag did with it\n", argv[0]); return 2; }
    }
    if (g_observe_udp && g_udp_queue_num < 0) g_udp_queue_num = g_queue_num + 1;
    if (g_observe_udp && g_udp_queue_num == g_queue_num) {
        fprintf(stderr, "TCP and UDP queue numbers must differ\n"); return 2;
    }
    if (logpath) {
        g_log = fopen(logpath, "a");
        if (!g_log) { perror("log"); return 1; }
        g_log_path = logpath;
        /* Overridable so rotation is testable without generating 8 MB. */
        const char *max = getenv("EXITLAG_LOG_MAX_BYTES");
        if (max && *max) {
            long v = strtol(max, NULL, 10);
            if (v > 0) g_log_max_bytes = v;
        }
    }

    /* Default the permitted peer to the desktop user who invoked sudo. Root is
       always permitted. If neither is knowable the helper still runs, but says
       so plainly -- a silent fallback here would mean an unauthenticated IPC
       socket owned by a root process. */
    if (g_allowed_peer_uid == (uid_t)-1) {
        /* Under sudo the desktop user is the one running ExitLag. Otherwise the
           owner is whoever started this helper: a helper running unprivileged
           can only usefully be driven by its own user, and defaulting to root
           there would lock out every non-root caller. */
        if (geteuid() != 0) {
            /* An unprivileged helper can only usefully be driven by its own
               user. SUDO_UID may be present and point somewhere else entirely
               (nested sudo rewrites it), so it is not consulted here. */
            g_allowed_peer_uid = getuid();
        } else {
            const char *sudo_uid = getenv("SUDO_UID");
            if (sudo_uid && *sudo_uid) g_allowed_peer_uid = (uid_t)strtoul(sudo_uid, NULL, 10);
            else g_allowed_peer_uid = getuid();
        }
    }
    log_line("IPC peer policy: uid 0 and uid %u may connect", (unsigned)g_allowed_peer_uid);

    if (!g_token_path[0]) {
        /* Root publishes under /run; an unprivileged helper (tests, IPC-only
           runs) uses the user's runtime dir, which only that user can read. */
        if (geteuid() == 0) {
            mkdir("/run/exitlag-linux", 0755);
            snprintf(g_token_path, sizeof(g_token_path), "/run/exitlag-linux/ipc.token");
        } else {
            const char *xdg = getenv("XDG_RUNTIME_DIR");
            if (xdg && *xdg) snprintf(g_token_path, sizeof(g_token_path), "%s/exitlag-ipc.token", xdg);
            else snprintf(g_token_path, sizeof(g_token_path), "/tmp/exitlag-ipc-%u.token", (unsigned)getuid());
        }
    }
    if (token_publish(g_allowed_peer_uid) != 0) {
        fprintf(stderr, "cannot publish the IPC session token; refusing to run unauthenticated\n");
        return 1;
    }
    atexit(token_remove);
    /*
     * A runner that forgets to pass --identity-map does not fail; it silently
     * runs without strict mode, so unmapped processes keep the spoofable argv
     * scan and nothing is translated. That is a security-relevant downgrade
     * with no visible symptom, and it has happened once already. If the
     * environment asked for a map, refuse to run without it.
     */
    if (!identity_map_path) {
        const char *wanted = getenv("EXITLAG_IDENTITY_MAP");
        if (wanted && *wanted) {
            fprintf(stderr,
                    "EXITLAG_IDENTITY_MAP is set (%s) but --identity-map was not passed.\n"
                    "Refusing to start: this would run without strict identity matching,\n"
                    "silently leaving every process unmapped and unrouted.\n"
                    "The runner is responsible for forwarding it; see backend/runtime-guard.sh.\n",
                    wanted);
            return 2;
        }
    }
    if (identity_map_path && load_identity_map(identity_map_path) != 0) return 2;

    /* Endpoint ids must be unique across helper restarts: reusing them would let
       stale ExitLag per-endpoint state leak between experiments. */
    {
        struct timespec seed;
        clock_gettime(CLOCK_REALTIME, &seed);
        g_next_endpoint = 0x100000000ULL + ((unsigned long long)seed.tv_sec << 20);
        log_line("endpoint id base = 0x%llx", g_next_endpoint);
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    g_listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(g_listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    sa.sin_port = htons(port);
    if (bind(g_listen_fd, (struct sockaddr *)&sa, sizeof(sa)) < 0 || listen(g_listen_fd, 4) < 0) {
        log_line("cannot listen on 127.0.0.1:%u: %s", port, strerror(errno));
        return 1;
    }
    log_line("listening for shim on 127.0.0.1:%u", port);

    if (g_no_queue) {
        log_line("IPC-only mode: netfilter is untouched, no packets are observed");
    } else {
    g_nl = mnl_socket_open(NETLINK_NETFILTER);
    if (!g_nl) { log_line("mnl_socket_open: %s (need CAP_NET_ADMIN)", strerror(errno)); return 1; }
    {
        int requested = 4 * 1024 * 1024;
        int configured = 0;
#ifdef SO_RCVBUFFORCE
        configured = setsockopt(mnl_socket_get_fd(g_nl), SOL_SOCKET, SO_RCVBUFFORCE,
                                &requested, sizeof(requested)) == 0;
#endif
        if (!configured &&
            setsockopt(mnl_socket_get_fd(g_nl), SOL_SOCKET, SO_RCVBUF,
                       &requested, sizeof(requested)) < 0)
            log_line("netlink receive-buffer request failed: %s", strerror(errno));
        int actual = 0;
        socklen_t actual_len = sizeof(actual);
        if (getsockopt(mnl_socket_get_fd(g_nl), SOL_SOCKET, SO_RCVBUF,
                       &actual, &actual_len) == 0)
            log_line("netlink receive buffer=%d bytes", actual);
    }
    if (mnl_socket_bind(g_nl, 0, MNL_SOCKET_AUTOPID) < 0) { log_line("mnl_socket_bind: %s", strerror(errno)); return 1; }
    g_portid = mnl_socket_get_portid(g_nl);

    if (nfq_bind_one(g_queue_num) < 0) {
        log_line("failed to bind NFQUEUE %d: %s", g_queue_num, strerror(errno));
        return 1;
    }
    log_line("bound NFQUEUE %d (%s)", g_queue_num,
             g_apply_tcp ? "active TCP redirect; all failures ACCEPT unchanged" :
                           "observation only; every packet ACCEPTed unchanged");
    if (g_observe_udp) {
        if (nfq_bind_one(g_udp_queue_num) < 0) {
            log_line("failed to bind UDP NFQUEUE %d: %s", g_udp_queue_num, strerror(errno));
            nfq_config(g_queue_num, NFQNL_CFG_CMD_UNBIND, 0, 0);
            return 1;
        }
        log_line("bound UDP NFQUEUE %d (%s; overflow fail-open)", g_udp_queue_num,
                 g_apply_udp ? "active narrow post/reinject subset" :
                               "observation only; every datagram ACCEPTed unchanged");
    }
    }

    static unsigned char buf[EXL_MAX_PAYLOAD] __attribute__((aligned(8)));
    time_t last_heartbeat = 0;
    while (!g_stop) {
        struct pollfd pfds[3];
        int n = 0;
        int nl_index = -1;
        if (!g_no_queue) { nl_index = n; pfds[n].fd = mnl_socket_get_fd(g_nl); pfds[n].events = POLLIN; pfds[n].revents = 0; n++; }
        int listen_index = n;
        pfds[n].fd = g_listen_fd; pfds[n].events = POLLIN; pfds[n].revents = 0; n++;
        int shim_index = -1;
        if (g_shim_fd >= 0) { shim_index = n; pfds[n].fd = g_shim_fd; pfds[n].events = POLLIN; pfds[n].revents = 0; n++; }

        int r = poll(pfds, (nfds_t)n, 1000);
        if (r < 0) { if (errno == EINTR) continue; break; }

        if (nl_index >= 0 && (pfds[nl_index].revents & POLLIN)) {
            ssize_t len = mnl_socket_recvfrom(g_nl, buf, sizeof(buf));
            if (len < 0) {
                if (errno == ENOBUFS) { log_line("kernel queue overrun (ENOBUFS); queue configured fail-open"); continue; }
                if (errno == EINTR) continue;
                log_line("recv error: %s", strerror(errno));
                break;
            }
            if (mnl_cb_run(buf, (size_t)len, 0, g_portid, on_packet, NULL) < 0)
                log_line("cb_run: %s", strerror(errno));
        }
        if (pfds[listen_index].revents & POLLIN) {
            accept_shim();
            if (g_selftest && g_shim_fd >= 0) run_self_test(g_selftest);
        }
        if (shim_index >= 0 && (pfds[shim_index].revents & (POLLIN | POLLHUP | POLLERR))) {
            /* unsolicited data or EOF from the shim */
            struct exl_header head;
            if (read_exact(g_shim_fd, &head, sizeof(head), 0) != 0) drop_shim("eof");
            else if (head.magic != EXL_MAGIC || head.length > EXL_MAX_PAYLOAD) drop_shim("bad framing");
            else {
                static unsigned char tmp[EXL_MAX_PAYLOAD];
                if (head.length && read_exact(g_shim_fd, tmp, head.length, 200) != 0) drop_shim("short payload");
                else if (head.type == EXL_MSG_HELLO && head.length >= sizeof(struct exl_hello)) {
                    const struct exl_hello *hello = (const struct exl_hello *)tmp;
                    g_shim_windows_pid = hello->shim_process_id;
                    struct exl_hello_ack ack = { EXL_PROTOCOL_VERSION, (unsigned int)getpid(), 0 };
                    send_frame(g_shim_fd, EXL_MSG_HELLO_ACK, &ack, sizeof(ack));
                    log_line("hello: shim protocol=%u exitlag_windows_pid=%u", hello->version, hello->shim_process_id);
                } else handle_nested_message(&head, tmp);
            }
        }
        if (g_shim_fd >= 0) {
            struct timespec heartbeat_now;
            clock_gettime(CLOCK_MONOTONIC, &heartbeat_now);
            if (heartbeat_now.tv_sec - last_heartbeat >= 2) {
                if (send_frame(g_shim_fd, EXL_MSG_PING, NULL, 0) != 0)
                    drop_shim("heartbeat send failed");
                else
                    last_heartbeat = heartbeat_now.tv_sec;
            }
        } else {
            last_heartbeat = 0;
        }
        if (g_observe_udp) udp_reap_endpoints();
    }

    log_line("shutting down");
    if (!g_no_queue) {
        if (g_observe_udp) nfq_config(g_udp_queue_num, NFQNL_CFG_CMD_UNBIND, 0, 0);
        nfq_config(g_queue_num, NFQNL_CFG_CMD_UNBIND, 0, 0);
        mnl_socket_close(g_nl);
    }
    if (g_shim_fd >= 0) close(g_shim_fd);
    if (g_listen_fd >= 0) close(g_listen_fd);
    return 0;
}
