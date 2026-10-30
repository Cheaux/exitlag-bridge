// The current client imports the SDK's Windows driver-management entry points.
// macOS routing belongs to the native bridge; this shim cannot install or remove
// a Windows filtering driver. Export the documented ABI and report unsupported
// operations explicitly, instead of preventing the Windows loader from starting.
typedef unsigned long DWORD;
typedef void *HANDLE;
typedef int BOOL;
#define WINAPI __stdcall
extern "C" __declspec(dllimport) void WINAPI SetLastError(DWORD);
extern "C" __declspec(dllimport) void WINAPI OutputDebugStringA(const char *);
extern "C" __declspec(dllimport) HANDLE WINAPI GetModuleHandleW(const wchar_t *);
extern "C" __declspec(dllimport) DWORD WINAPI GetEnvironmentVariableW(const wchar_t *, wchar_t *, DWORD);
extern "C" __declspec(dllimport) BOOL WINAPI VirtualProtect(void *, unsigned __int64, DWORD, DWORD *);
extern "C" __declspec(dllimport) HANDLE WINAPI CreateFileW(const wchar_t *, DWORD, DWORD, void *, DWORD, DWORD, HANDLE);
extern "C" __declspec(dllimport) BOOL WINAPI WriteFile(HANDLE, const void *, DWORD, DWORD *, void *);
extern "C" __declspec(dllimport) BOOL WINAPI CloseHandle(HANDLE);
extern "C" BOOL WINAPI DllMain(HANDLE, DWORD, void *);

static unsigned int WINAPI unavailable_multimedia_timer(unsigned int, unsigned int,
    void *, unsigned __int64, unsigned int)
{
    // Qt 6.10.3's registerTimer checks a zero return and uses
    // SetCoalescableTimer / SetTimer instead. Preserve that documented failure
    // contract; do not emulate successful precision timers.
    SetLastError(50);
    return 0;
}

static bool ascii_equal(const char *a, const char *b)
{
    for (unsigned int i = 0; i < 64; ++i) {
        unsigned char x = a[i], y = b[i];
        if (x >= 'A' && x <= 'Z') x += 'a' - 'A';
        if (y >= 'A' && y <= 'Z') y += 'a' - 'A';
        if (x != y) return false;
        if (!x) return true;
    }
    return false;
}

static bool install_qt_timer_fallback()
{
    auto base = (unsigned char *)GetModuleHandleW(L"Qt6Core.dll");
    if (!base || *(unsigned short *)base != 0x5a4d) return false;
    DWORD nt = *(DWORD *)(base + 60);
    if (nt > 4096 || *(DWORD *)(base + nt) != 0x4550) return false;
    auto opt = base + nt + 24;
    if (*(unsigned short *)opt != 0x20b) return false;
    DWORD image_size = *(DWORD *)(opt + 56);
    DWORD imports = *(DWORD *)(opt + 120);
    if (!imports || image_size < 512 || imports >= image_size - 20) return false;
    for (unsigned int i = 0; i < 512 && imports + i * 20 < image_size - 20; ++i) {
        auto entry = (DWORD *)(base + imports + i * 20);
        if (!entry[0] && !entry[3] && !entry[4]) break;
        if (!entry[0] || entry[3] >= image_size - 64 || entry[4] >= image_size - 8) continue;
        if (!ascii_equal((const char *)(base + entry[3]), "winmm.dll")) continue;
        for (unsigned int j = 0; j < 4096; ++j) {
            unsigned __int64 offset = entry[0] + 8ULL * j;
            unsigned __int64 iat_offset = entry[4] + 8ULL * j;
            if (offset >= image_size - 8 || iat_offset >= image_size - 8) return false;
            unsigned __int64 name = *(unsigned __int64 *)(base + offset);
            if (!name) break;
            if (name >> 63 || name >= image_size - 66) continue;
            if (!ascii_equal((const char *)(base + name + 2), "timeSetEvent")) continue;
            auto slot = (unsigned __int64 *)(base + iat_offset);
            DWORD previous, unused;
            if (!VirtualProtect(slot, 8, 4, &previous)) return false;
            *slot = (unsigned __int64)&unavailable_multimedia_timer;
            VirtualProtect(slot, 8, previous, &unused);
            return true;
        }
    }
    return false;
}

extern "C" BOOL WINAPI MacDllMain(HANDLE module, DWORD reason, void *reserved)
{
    BOOL result = DllMain(module, reason, reserved);
    wchar_t option[8];
    if (reason == 1 && GetEnvironmentVariableW(L"EXITLAG_MACOS_SAFE_QT_TIMERS", option, 8)
        && option[0] == L'1') {
        bool installed = install_qt_timer_fallback();
        const char *message = installed ? "macos.qt.timer_fallback installed=1\n"
                                       : "macos.qt.timer_fallback installed=0\n";
        OutputDebugStringA(message);
        wchar_t path[1024];
        DWORD length = GetEnvironmentVariableW(L"EXITLAG_NFAPI_LOG", path, 1024);
        if (length && length < 1024) {
            HANDLE file = CreateFileW(path, 4, 3, 0, 4, 0x80, 0);
            if (file != (HANDLE)(__int64)-1) {
                DWORD written, size = 0;
                while (message[size]) ++size;
                WriteFile(file, message, size, &written, 0);
                CloseHandle(file);
            }
        }
    }
    return result;
}

typedef enum _NF_STATUS {
    NF_STATUS_SUCCESS = 0,
    NF_STATUS_FAIL = -1,
} NF_STATUS;

namespace nfapi {
__declspec(dllexport) NF_STATUS nf_registerDriver(const char *)
{
    OutputDebugStringA("ExitLag Mac bridge: Windows driver registration is unsupported.\n");
    SetLastError(50); // ERROR_NOT_SUPPORTED
    return NF_STATUS_FAIL;
}

__declspec(dllexport) NF_STATUS nf_unRegisterDriver(const char *)
{
    OutputDebugStringA("ExitLag Mac bridge: Windows driver removal is unsupported.\n");
    SetLastError(50);
    return NF_STATUS_FAIL;
}
}
