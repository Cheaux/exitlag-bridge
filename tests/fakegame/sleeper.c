/* Minimal long-lived Windows process used only to test ExitLag's game-process
   detection (CreateToolhelp32Snapshot/Process32NextW). Does nothing else: no
   sockets, no files, no registry. Renamed copies stand in for game clients. */
#include <windows.h>
int WINAPI wWinMain(HINSTANCE a, HINSTANCE b, PWSTR c, int d)
{
    (void)a; (void)b; (void)c; (void)d;
    for (;;) Sleep(1000);
    return 0;
}
