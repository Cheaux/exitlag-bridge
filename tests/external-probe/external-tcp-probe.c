// Minimal Windows TCP probe for the external-prefix validation.
//
// Runs in a DISPOSABLE second Wine prefix -- not ExitLag's -- and opens one
// TCP connection to a destination given on the command line. It exists to
// prove that a Windows process in a different prefix can be attributed and
// routed by the Linux backend, so it deliberately does nothing else.
//
//   external-tcp-probe.exe <ipv4> <port> [hold_ms]

typedef unsigned short u16;
typedef unsigned long  u32;
typedef unsigned __int64 u64;
typedef int            SOCKET_T;

#define WINAPI __stdcall
#define INVALID_SOCKET_ (~(SOCKET_T)0)

__declspec(dllimport) int  WINAPI WSAStartup(u16, void *);
__declspec(dllimport) int  WINAPI WSACleanup(void);
__declspec(dllimport) SOCKET_T WINAPI socket(int, int, int);
__declspec(dllimport) int  WINAPI connect(SOCKET_T, const void *, int);
__declspec(dllimport) int  WINAPI closesocket(SOCKET_T);
__declspec(dllimport) u32  WINAPI inet_addr(const char *);
__declspec(dllimport) u16  WINAPI htons(u16);
__declspec(dllimport) int  WINAPI WSAGetLastError(void);
__declspec(dllimport) int  WINAPI send(SOCKET_T, const char *, int, int);
__declspec(dllimport) int  WINAPI recv(SOCKET_T, char *, int, int);

__declspec(dllimport) void * WINAPI GetStdHandle(u32);
__declspec(dllimport) int    WINAPI WriteFile(void *, const void *, u32, u32 *, void *);
__declspec(dllimport) void   WINAPI Sleep(u32);
__declspec(dllimport) void   WINAPI ExitProcess(u32);
__declspec(dllimport) char * WINAPI GetCommandLineA(void);
__declspec(dllimport) u32    WINAPI GetCurrentProcessId(void);

static void put(const char *s)
{
    u32 n = 0; while (s[n]) ++n;
    u32 written = 0;
    WriteFile(GetStdHandle((u32)-11), s, n, &written, 0);
}

static void put_u32(u32 v)
{
    char buf[16]; int i = 15; buf[i] = 0;
    if (!v) buf[--i] = '0';
    while (v) { buf[--i] = (char)('0' + (v % 10)); v /= 10; }
    put(buf + i);
}

// Split the raw command line; avoids pulling in the CRT argv machinery.
static const char *next_token(const char **p)
{
    const char *s = *p;
    while (*s == ' ' || *s == '\t') ++s;
    if (!*s) { *p = s; return 0; }
    static char store[4][256];
    static int slot;
    char *out = store[slot & 3]; ++slot;
    int n = 0;
    if (*s == '"') {
        ++s;
        while (*s && *s != '"' && n < 255) out[n++] = *s++;
        if (*s == '"') ++s;
    } else {
        while (*s && *s != ' ' && *s != '\t' && n < 255) out[n++] = *s++;
    }
    out[n] = 0; *p = s; return out;
}

static u32 parse_u32(const char *s)
{
    u32 v = 0; while (*s >= '0' && *s <= '9') v = v * 10 + (u32)(*s++ - '0');
    return v;
}

struct sockaddr_in_ { short family; u16 port; u32 addr; char zero[8]; };

int mainCRTStartup(void)
{
    const char *cl = GetCommandLineA();
    next_token(&cl);                              // argv[0]
    const char *ip   = next_token(&cl);
    const char *port = next_token(&cl);
    const char *hold = next_token(&cl);
    if (!ip || !port) { put("usage: external-tcp-probe.exe <ipv4> <port> [hold_ms]\n"); ExitProcess(2); }

    put("external-probe windows_pid="); put_u32(GetCurrentProcessId()); put("\n");

    char wsadata[512];
    if (WSAStartup(0x0202, wsadata) != 0) { put("WSAStartup failed\n"); ExitProcess(3); }

    SOCKET_T s = socket(2 /*AF_INET*/, 1 /*SOCK_STREAM*/, 6 /*IPPROTO_TCP*/);
    if (s == INVALID_SOCKET_) { put("socket failed\n"); WSACleanup(); ExitProcess(4); }

    struct sockaddr_in_ sa;
    for (int i = 0; i < 8; ++i) sa.zero[i] = 0;
    sa.family = 2; sa.port = htons((u16)parse_u32(port)); sa.addr = inet_addr(ip);

    put("connecting to "); put(ip); put(":"); put(port); put("\n");
    if (connect(s, &sa, (int)sizeof(sa)) != 0) {
        put("connect failed err="); put_u32((u32)WSAGetLastError()); put("\n");
        closesocket(s); WSACleanup(); ExitProcess(5);
    }
    put("connected\n");
    // Do real I/O. Wine appears to materialise the socket fd in the client
    // process only once it is actually used; a connect-and-idle socket stays
    // owned solely by wineserver, which is not how a real game behaves.
    static const char hello[] = "\x00\x08\x00\x00probe";
    int sent = send(s, hello, (int)sizeof(hello) - 1, 0);
    put("sent="); put_u32((u32)sent); put("\n");
    char rbuf[64];
    int got = recv(s, rbuf, (int)sizeof(rbuf), 0);
    put("recv="); put_u32((u32)got); put("\n");
    Sleep(hold ? parse_u32(hold) : 4000);
    closesocket(s); WSACleanup();
    put("done\n");
    ExitProcess(0);
    return 0;
}
