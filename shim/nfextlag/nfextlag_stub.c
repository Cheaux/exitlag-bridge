/*
 * Presence-only stand-in for ExitLag's NetFilter SDK kernel driver image.
 *
 * ExitLag.exe never opens this driver directly; it talks to the driver through
 * nfapi.dll, which this project replaces. The client only needs:
 *   1. a kernel-driver service named "nfextlag" whose status is SERVICE_RUNNING
 *      (advapi32 OpenServiceA + QueryServiceStatus, StartServiceA if stopped);
 *   2. C:\windows\system32\drivers\nfextlag.sys carrying VS_FIXEDFILEINFO
 *      file version 1.7.1.6 (version.dll GetFileVersionInfoW + VerQueryValueW "\\").
 *
 * Wine loads this image inside winedevice.exe and calls DriverEntry. The stub
 * has no imports, creates no device objects, registers no dispatch routines,
 * and performs no I/O. It exists only so the service can reach RUNNING.
 */

typedef long NTSTATUS;
#define STATUS_SUCCESS ((NTSTATUS)0)

NTSTATUS __stdcall DriverEntry(void *driver_object, void *registry_path)
{
    (void)driver_object;
    (void)registry_path;
    return STATUS_SUCCESS;
}
