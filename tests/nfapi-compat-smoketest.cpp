// Synthetic regression for the Wine missing-import crash guards.
//
// ExitLag imports functions Wine does not export. Wine's loader resolves those
// to stubs that raise the non-continuable EXCEPTION_WINE_STUB (0x80000100) when
// called, which kills the process -- that is what crashed the client when the
// PC Boost and DNS pages were opened.
//
// This executable deliberately imports two of those functions (one per affected
// DLL) and calls them before nf_init. Without the guard the process dies on the
// first call. With the guard each returns ERROR_NOT_SUPPORTED (50) normally,
// because nfapi.dll patched this image's import table from its process-attach
// event, before the entry point ran.

typedef unsigned long DWORD;
#define WINAPI __stdcall

extern "C" {
// IPHLPAPI: the exact function the captured minidump blamed.
__declspec(dllimport) DWORD WINAPI GetInterfaceDnsSettings(void *interface_guid, void *settings);
// POWRPROF: the PC Boost power-scheme path.
__declspec(dllimport) DWORD WINAPI PowerWriteDCValueIndex(void *root, const void *scheme,
                                                          const void *subgroup, const void *setting,
                                                          DWORD value);
}

typedef enum _NF_STATUS { NF_STATUS_SUCCESS = 0, NF_STATUS_FAIL = -1 } NF_STATUS;

namespace nfapi {
class NF_EventHandler;
__declspec(dllimport) NF_STATUS nf_init(const char *, NF_EventHandler *);
}

extern "C" int mainCRTStartup()
{
    unsigned char guid[16];
    for (unsigned int i = 0; i < sizeof(guid); ++i) guid[i] = 0;

    // Reaching the next line at all proves the stub was replaced: an unguarded
    // import raises a non-continuable exception inside the call.
    unsigned char settings[128];
    for (unsigned int i = 0; i < sizeof(settings); ++i) settings[i] = 0;
    if (GetInterfaceDnsSettings(guid, settings) != 50) return 1;

    if (PowerWriteDCValueIndex(0, guid, 0, guid, 0) != 50) return 2;

    // Log the install state. nf_init keeps its default failure policy here, so
    // no helper thread, filtering state or callback is started.
    nfapi::nf_init("compat-smoketest", 0);
    return 0;
}
