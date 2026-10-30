/* Experimental Wine-owned socket adapter for a real native Mac game.
 * No game implementation, credential handling, code injection or driver access.
 * The authenticated local bridge reserves a Winsock source port, runs ExitLag's
 * callback, then supplies its loopback redirect. Only that loopback destination
 * is accepted. A six-second native heartbeat lease bounds the process lifetime.
 */
typedef unsigned long DWORD;
typedef unsigned short WCHAR;
typedef unsigned long long U64, SOCKET;
typedef void *HANDLE;
typedef struct { DWORD low, high; } FILETIME;
typedef struct { DWORD attributes; FILETIME created, accessed, written; DWORD high, low; } FILEDATA;
typedef struct { unsigned short family, port; DWORD address; unsigned char zero[8]; } ADDRESS;
#define IMPORT __declspec(dllimport)
#define INVALID_SOCKET ((SOCKET)-1)
IMPORT DWORD __stdcall GetCurrentProcessId(void);
IMPORT DWORD __stdcall GetEnvironmentVariableW(const WCHAR *, WCHAR *, DWORD);
IMPORT HANDLE __stdcall CreateFileW(const WCHAR *, DWORD, DWORD, void *, DWORD, DWORD, HANDLE);
IMPORT int __stdcall ReadFile(HANDLE, void *, DWORD, DWORD *, void *);
IMPORT int __stdcall WriteFile(HANDLE, const void *, DWORD, DWORD *, void *);
IMPORT int __stdcall CloseHandle(HANDLE);
IMPORT int __stdcall GetFileAttributesExW(const WCHAR *, int, FILEDATA *);
IMPORT void __stdcall GetSystemTimeAsFileTime(FILETIME *);
IMPORT void __stdcall Sleep(DWORD);
IMPORT void __stdcall ExitProcess(DWORD);
IMPORT HANDLE __stdcall GetProcessHeap(void);
IMPORT void *__stdcall HeapAlloc(HANDLE, DWORD, U64);
IMPORT int __stdcall HeapFree(HANDLE, DWORD, void *);
IMPORT HANDLE __stdcall CreateThread(void *, U64, DWORD (__stdcall *)(void *), void *, DWORD, DWORD *);
IMPORT DWORD __stdcall WaitForSingleObject(HANDLE, DWORD);
IMPORT int __stdcall WSAStartup(unsigned short, void *);
IMPORT SOCKET __stdcall socket(int, int, int);
IMPORT int __stdcall bind(SOCKET, const void *, int);
IMPORT int __stdcall listen(SOCKET, int);
IMPORT SOCKET __stdcall accept(SOCKET, void *, int *);
IMPORT int __stdcall connect(SOCKET, const void *, int);
IMPORT int __stdcall getsockname(SOCKET, void *, int *);
IMPORT int __stdcall setsockopt(SOCKET, int, int, const void *, int);
IMPORT int __stdcall recv(SOCKET, char *, int, int);
IMPORT int __stdcall send(SOCKET, const char *, int, int);
IMPORT int __stdcall shutdown(SOCKET, int);
IMPORT int __stdcall closesocket(SOCKET);
static WCHAR pid_path[1024], heartbeat_path[1024], token_path[1024];
static unsigned char token[32];
static long active;
static HANDLE heap;
static U64 ticks(FILETIME f) { return ((U64)f.high << 32) | f.low; }
static int hex(unsigned char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}
static int exact(SOCKET s, void *buffer, int length, int writing) {
    char *bytes = buffer;
    while (length) {
        int n = writing ? send(s, bytes, length, 0) : recv(s, bytes, length, 0);
        if (n <= 0) return 0;
        bytes += n; length -= n;
    }
    return 1;
}
typedef struct { SOCKET local, relay; } Connection;
static void transfer(SOCKET source, SOCKET dest) {
    char buffer[16384];
    for (;;) {
        int n = recv(source, buffer, sizeof(buffer), 0);
        if (n <= 0 || !exact(dest, buffer, n, 1)) break;
    }
    shutdown(dest, 1);
}
static DWORD __stdcall inbound(void *arg) {
    Connection *c = arg; transfer(c->relay, c->local); return 0;
}
static DWORD __stdcall worker(void *arg) {
    Connection *c = arg; unsigned char request[40]; DWORD timeout = 5000;
    setsockopt(c->local, 0xffff, 0x1006, &timeout, 4);
    setsockopt(c->local, 0xffff, 0x1005, &timeout, 4);
    if (!exact(c->local, request, sizeof(request), 0)) goto done;
    if (request[0] != 'E' || request[1] != 'W' || request[2] != 'S' || request[3] != '1') goto done;
    unsigned int different = 0;
    for (int i = 0; i < 32; ++i) different |= token[i] ^ request[4 + i];
    if (different) goto done;
    c->relay = socket(2, 1, 6);
    if (c->relay == INVALID_SOCKET) goto done;
    ADDRESS local = {2, 0, 0, {0}};
    for (int i = 0; i < 4; ++i) ((unsigned char *)&local.address)[i] = request[36 + i];
    if (bind(c->relay, &local, sizeof(local))) goto done;
    int size = sizeof(local);
    if (getsockname(c->relay, &local, &size) || size != sizeof(local)) goto done;
    DWORD pid = GetCurrentProcessId();
    if (!exact(c->local, &pid, 4, 1) || !exact(c->local, &local, sizeof(local), 1)) goto done;
    ADDRESS target;
    if (!exact(c->local, &target, sizeof(target), 0)) goto done;
    if (target.family != 2 || !target.port || ((unsigned char *)&target.address)[0] != 127) goto done;
    DWORD ok = connect(c->relay, &target, sizeof(target)) == 0;
    if (!exact(c->local, &ok, 4, 1) || !ok) goto done;
    timeout = 0;
    setsockopt(c->local, 0xffff, 0x1006, &timeout, 4);
    setsockopt(c->local, 0xffff, 0x1005, &timeout, 4);
    DWORD one = 1;
    setsockopt(c->local, 6, 1, &one, 4); setsockopt(c->relay, 6, 1, &one, 4);
    HANDLE thread = CreateThread(0, 0, inbound, c, 0, 0);
    if (!thread) goto done;
    transfer(c->local, c->relay);
    WaitForSingleObject(thread, 0xffffffff); CloseHandle(thread);
done:
    closesocket(c->local);
    if (c->relay != INVALID_SOCKET) closesocket(c->relay);
    HeapFree(heap, 0, c); __atomic_fetch_sub(&active, 1, __ATOMIC_RELAXED); return 0;
}
static DWORD __stdcall heartbeat(void *unused) {
    (void)unused;
    for (;;) {
        FILEDATA data; FILETIME now;
        if (!GetFileAttributesExW(heartbeat_path, 0, &data)) break;
        GetSystemTimeAsFileTime(&now);
        if (ticks(now) < ticks(data.written) || ticks(now) - ticks(data.written) > 60000000ULL) break;
        Sleep(250);
    }
    ExitProcess(0); return 0;
}
void entry(void) {
    WCHAR port_text[8]; unsigned int port = 51902;
    DWORD port_length = GetEnvironmentVariableW((const WCHAR *)L"EXITLAG_COMPANION_PORT", port_text, 8);
    if (port_length) {
        if (port_length != 5) ExitProcess(11);
        port = 0;
        for (DWORD i = 0; i < port_length; ++i) {
            if (port_text[i] < '0' || port_text[i] > '9') ExitProcess(11);
            port = port * 10 + port_text[i] - '0';
        }
        if (port != 51902 && port != 51903) ExitProcess(11);
    }
    DWORD p = GetEnvironmentVariableW((const WCHAR *)L"EXITLAG_COMPANION_PID_FILE", pid_path, 1024);
    DWORD h = GetEnvironmentVariableW((const WCHAR *)L"EXITLAG_COMPANION_HEARTBEAT", heartbeat_path, 1024);
    DWORD t = GetEnvironmentVariableW((const WCHAR *)L"EXITLAG_COMPANION_TOKEN_FILE", token_path, 1024);
    if (!p || p >= 1024 || !h || h >= 1024 || !t || t >= 1024) ExitProcess(2);
    HANDLE file = CreateFileW(token_path, 0x80000000, 1, 0, 3, 0x80, 0);
    char hex_token[65]; DWORD count = 0;
    if (file == (HANDLE)(U64)-1 || !ReadFile(file, hex_token, 65, &count, 0) || count != 64) ExitProcess(3);
    CloseHandle(file);
    for (int i = 0; i < 32; ++i) {
        int a = hex(hex_token[2 * i]), b = hex(hex_token[2 * i + 1]);
        if (a < 0 || b < 0) ExitProcess(4);
        token[i] = (a << 4) | b;
    }
    unsigned char wsa[512]; if (WSAStartup(0x202, wsa)) ExitProcess(5);
    SOCKET listener = socket(2, 1, 6); if (listener == INVALID_SOCKET) ExitProcess(6);
    ADDRESS address = {2, (unsigned short)((port >> 8) | (port << 8)), 0x0100007f, {0}};
    if (bind(listener, &address, sizeof(address)) || listen(listener, 16)) ExitProcess(7);
    heap = GetProcessHeap();
    file = CreateFileW(pid_path, 0x40000000, 1, 0, 2, 0x80, 0);
    if (file == (HANDLE)(U64)-1) ExitProcess(8);
    DWORD pid = GetCurrentProcessId(), written = 0; count = 0;
    char buffer[16], reverse[16];
    do { reverse[count++] = '0' + pid % 10; pid /= 10; } while (pid);
    for (DWORD i = 0; i < count; ++i) buffer[i] = reverse[count - i - 1];
    buffer[count++] = '\n';
    int ok = WriteFile(file, buffer, count, &written, 0); CloseHandle(file);
    if (!ok || written != count) ExitProcess(9);
    HANDLE monitor = CreateThread(0, 0, heartbeat, 0, 0, 0);
    if (!monitor) ExitProcess(10); CloseHandle(monitor);
    for (;;) {
        SOCKET s = accept(listener, 0, 0); if (s == INVALID_SOCKET) break;
        if (__atomic_fetch_add(&active, 1, __ATOMIC_RELAXED) >= 32) {
            __atomic_fetch_sub(&active, 1, __ATOMIC_RELAXED); closesocket(s); continue;
        }
        Connection *c = HeapAlloc(heap, 8, sizeof(Connection));
        if (!c) { __atomic_fetch_sub(&active, 1, __ATOMIC_RELAXED); closesocket(s); continue; }
        c->local = s; c->relay = INVALID_SOCKET;
        HANDLE thread = CreateThread(0, 0, worker, c, 0, 0);
        if (thread) CloseHandle(thread);
        else { HeapFree(heap, 0, c); closesocket(s); __atomic_fetch_sub(&active, 1, __ATOMIC_RELAXED); }
    }
    ExitProcess(0);
}
