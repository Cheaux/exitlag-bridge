/*
 * Wire protocol between the Windows-side nfapi shim (inside Wine) and the
 * native Linux helper (exitlag-netd).
 *
 * Transport: loopback TCP. Unix-domain sockets would be preferable, but Wine
 * 11.17's ws2_32 family map (dlls/ws2_32/unixlib.c:139) contains only
 * AF_UNSPEC/AF_INET/AF_INET6/AF_IPX/AF_IRDA -- a PE module under Wine cannot
 * create an AF_UNIX socket at all. ExitLag's own rule table allows loopback
 * unconditionally (rules 0-2), so this traffic is never a candidate for
 * interception and cannot create a proxy loop.
 *
 * Framing: every message is a fixed 12-byte header followed by `length` bytes
 * of payload. All integers are little-endian (both peers are x86-64).
 *
 *   u32 magic    'E','X','L','1'
 *   u16 type     EXL_MSG_*
 *   u16 flags    reserved, must be 0
 *   u32 length   payload byte count
 *
 * Requests carry a u64 `tag`; the matching reply echoes it, so several
 * requests can be in flight on one connection.
 */
#ifndef EXITLAG_PROTOCOL_H
#define EXITLAG_PROTOCOL_H

#define EXL_MAGIC          0x314c5845u   /* "EXL1" little-endian */
#define EXL_PROTOCOL_VERSION 3
#define EXL_DEFAULT_PORT   51900
#define EXL_MAX_PAYLOAD    131072

/* message types */
#define EXL_MSG_HELLO              0x0001  /* shim   -> helper */
#define EXL_MSG_HELLO_ACK          0x0002  /* helper -> shim   */
#define EXL_MSG_PING               0x0003  /* helper -> shim, no payload */
#define EXL_MSG_PONG               0x0004  /* shim   -> helper, no payload */
#define EXL_MSG_TCP_CONNECT_REQ    0x0010  /* helper -> shim   */
#define EXL_MSG_TCP_CONNECT_REPLY  0x0011  /* shim   -> helper */
#define EXL_MSG_RESOLVE_PID_REQ    0x0020  /* shim   -> helper */
#define EXL_MSG_RESOLVE_PID_REPLY  0x0021  /* helper -> shim   */
#define EXL_MSG_LOG                0x0030  /* either direction */
#define EXL_MSG_UDP_CREATED_REQ    0x0040  /* helper -> shim   */
#define EXL_MSG_UDP_CREATED_REPLY  0x0041  /* shim   -> helper */
#define EXL_MSG_UDP_CONNECT_REQ    0x0042  /* helper -> shim   */
#define EXL_MSG_UDP_CONNECT_REPLY  0x0043  /* shim   -> helper */
#define EXL_MSG_UDP_DATAGRAM_REQ   0x0044  /* helper -> shim   */
#define EXL_MSG_UDP_DATAGRAM_REPLY 0x0045  /* shim   -> helper */
#define EXL_MSG_UDP_POST           0x0046  /* shim   -> helper, nested */
#define EXL_MSG_UDP_STATE          0x0047  /* shim   -> helper, nested */
#define EXL_MSG_UDP_CLOSED_REQ     0x0048  /* helper -> shim   */
#define EXL_MSG_UDP_CLOSED_REPLY   0x0049  /* shim   -> helper */

#define EXL_DIR_IN  1
#define EXL_DIR_OUT 2

/* API ids carried by EXL_MSG_UDP_POST / EXL_MSG_UDP_STATE. */
#define EXL_UDP_API_POST_SEND          1
#define EXL_UDP_API_POST_RECEIVE       2
#define EXL_UDP_API_ADD_REMOTE_RULE    3
#define EXL_UDP_API_DISABLE_FILTERING  4
#define EXL_UDP_API_ENABLE_FILTERING   5
#define EXL_UDP_API_SET_FLOWCTL        6

/* verdict values returned by the shim for an event */
#define EXL_VERDICT_UNCHANGED 0  /* pass the packet through untouched  */
#define EXL_VERDICT_MUTATED   1  /* ExitLag changed the structure      */
#define EXL_VERDICT_NO_HANDLER 2 /* no ExitLag handler registered yet   */

#pragma pack(push, 1)

struct exl_header {
    unsigned int   magic;
    unsigned short type;
    unsigned short flags;
    unsigned int   length;
};

/*
 * Session token length in bytes. 32 bytes of kernel entropy: large enough that
 * guessing is not a strategy, small enough to stay in one fixed-size handshake.
 */
#define EXL_TOKEN_BYTES 32

struct exl_hello {
    unsigned int       version;
    unsigned int       shim_process_id;   /* ExitLag's Windows PID */
    unsigned long long capabilities;      /* reserved */
    /*
     * Proof that the sender can read the per-session token file, which only the
     * owning user can. The uid check alone cannot distinguish the real shim from
     * any other process of the same user; this can. Compared in constant time
     * and never logged.
     */
    unsigned char      token[EXL_TOKEN_BYTES];
};

struct exl_hello_ack {
    unsigned int       version;
    unsigned int       helper_pid;        /* Linux PID of exitlag-netd */
    unsigned long long capabilities;      /* reserved */
};

/*
 * A candidate outbound TCP connection observed on Linux. Addresses are carried
 * in the same 28-byte sockaddr form the NetFilter SDK uses, so the shim can
 * copy them straight into NF_TCP_CONN_INFO without reinterpretation:
 * family (2 bytes, 2=AF_INET / 23=AF_INET6), port (2 bytes, network order),
 * then the address bytes.
 */
struct exl_tcp_connect_req {
    unsigned long long tag;
    unsigned long long endpoint_id;
    unsigned int       process_id;        /* Windows PID if known, else 0 */
    unsigned int       filtering_flag;    /* what the rules would indicate */
    unsigned char      direction;         /* NF_D_IN / NF_D_OUT           */
    unsigned char      reserved[3];
    unsigned short     ip_family;
    unsigned char      local_address[28];
    unsigned char      remote_address[28];
};

struct exl_tcp_connect_reply {
    unsigned long long tag;
    unsigned int       verdict;           /* EXL_VERDICT_* */
    unsigned int       filtering_flag;    /* value after the callback */
    unsigned char      remote_address[28];/* value after the callback */
    unsigned char      mutated_flag;      /* 1 if filtering_flag changed */
    unsigned char      mutated_remote;    /* 1 if remote_address changed */
    unsigned char      reserved[2];
};

struct exl_resolve_pid_req {
    unsigned long long tag;
    unsigned int       process_id;        /* Linux PID */
    unsigned int       reserved;
};

/* path is UTF-8, `path_length` bytes, not NUL terminated; 0 = unresolved */
struct exl_resolve_pid_reply {
    unsigned long long tag;
    unsigned int       path_length;
    unsigned int       reserved;
};

/* One endpoint is stable for the lifetime of a local UDP socket. */
struct exl_udp_lifecycle_req {
    unsigned long long tag;
    unsigned long long endpoint_id;
    unsigned int       process_id;       /* Linux PID; shim prefers Windows owner */
    unsigned short     ip_family;
    unsigned char      local_address[28];
};

struct exl_udp_lifecycle_reply {
    unsigned long long tag;
    unsigned int       verdict;
    unsigned int       process_id;       /* effective Windows/Linux PID used */
    unsigned short     ip_family;
    unsigned char      local_address[28];
    unsigned char      mutated_info;
    unsigned char      reserved[3];
};

struct exl_udp_connect_req {
    unsigned long long tag;
    unsigned long long endpoint_id;
    unsigned int       process_id;
    unsigned int       filtering_flag;
    unsigned short     ip_family;
    unsigned char      local_address[28];
    unsigned char      remote_address[28];
};

struct exl_udp_connect_reply {
    unsigned long long tag;
    unsigned int       verdict;
    unsigned int       process_id;
    unsigned int       filtering_flag;
    unsigned char      remote_address[28];
    unsigned char      mutated_flag;
    unsigned char      mutated_remote;
    unsigned char      reserved[2];
};

/* options bytes followed by payload bytes. */
struct exl_udp_datagram_req {
    unsigned long long tag;
    unsigned long long endpoint_id;
    unsigned int       process_id;
    unsigned char      direction;
    unsigned char      reserved;
    unsigned short     ip_family;
    unsigned char      local_address[28];
    unsigned char      remote_address[28];
    unsigned int       option_flags;
    unsigned int       options_length;
    unsigned int       payload_length;
};

struct exl_udp_datagram_reply {
    unsigned long long tag;
    unsigned int       verdict;
    unsigned int       post_count;
    unsigned int       remote_rule_count;
    unsigned int       control_count;
    unsigned char      mutated_remote;
    unsigned char      mutated_payload;
    unsigned char      mutated_options;
    unsigned char      reserved;
};

/* options bytes followed by payload bytes. Sent while a datagram callback is active. */
struct exl_udp_post {
    unsigned long long event_tag;
    unsigned long long endpoint_id;
    unsigned int       api;
    int                status;
    unsigned char      remote_address[28];
    unsigned int       option_flags;
    unsigned int       options_length;
    unsigned int       payload_length;
};

/* `data_length` trailing bytes: 20 for ADD_REMOTE_RULE, zero otherwise. */
struct exl_udp_state {
    unsigned long long event_tag;
    unsigned long long endpoint_id;
    unsigned int       api;
    int                status;
    unsigned int       value;             /* flow-control handle when applicable */
    unsigned int       data_length;
};

#pragma pack(pop)

#endif /* EXITLAG_PROTOCOL_H */
