// Research-only transport lifetime test. nf_init starts the shim IPC thread;
// the process stays alive long enough for a silent listener timeout and retry.
typedef unsigned long DWORD;
#define WINAPI __stdcall

extern "C" __declspec(dllimport) void WINAPI Sleep(DWORD);

typedef enum _NF_STATUS { NF_STATUS_SUCCESS = 0, NF_STATUS_FAIL = -1 } NF_STATUS;

namespace nfapi {
class NF_EventHandler;
__declspec(dllimport) NF_STATUS nf_init(const char *, NF_EventHandler *);
__declspec(dllimport) void nf_free();
}

extern "C" int mainCRTStartup()
{
    if (nfapi::nf_init("reconnect-smoketest", 0) != NF_STATUS_SUCCESS) return 1;
    Sleep(20000);
    nfapi::nf_free();
    return 0;
}
