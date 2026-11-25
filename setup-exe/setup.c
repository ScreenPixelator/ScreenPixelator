/* Screen Pixelator setup: one .exe that carries the program files.
 * It unpacks them to a temporary folder, runs install.ps1 in this console
 * window, removes the temporary folder and waits for Enter.
 *
 * Layout of the file: [this program][payload][8-byte magic][8-byte payload offset]
 * payload = repeated { uint32 name length, name (UTF-8), uint64 size, bytes }
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define MAGIC "SPXSETUP"
#define MAX_FILES 32

static wchar_t g_dir[MAX_PATH * 2];
static wchar_t g_files[MAX_FILES][MAX_PATH * 2];
static int g_count = 0;

static void cleanup(void) {
    for (int i = 0; i < g_count; i++) DeleteFileW(g_files[i]);
    if (g_dir[0]) RemoveDirectoryW(g_dir);
}

static int finish(int code) {
    if (code == 0) {   /* all went well: close by itself */
        printf("\nDone. This window closes in 5 seconds...");
        fflush(stdout);
        Sleep(5000);
        return 0;
    }
    printf("\nSomething went wrong (see above). Press Enter to close this window...");
    fflush(stdout);
    getchar();
    return code;
}

static int fail(const char *msg) {
    printf("\nSetup error: %s\n", msg);
    cleanup();
    return finish(1);
}

static int read_exact(HANDLE h, void *buf, DWORD n) {
    DWORD got = 0;
    while (n) {
        if (!ReadFile(h, buf, n, &got, NULL) || got == 0) return 0;
        buf = (char *)buf + got;
        n -= got;
    }
    return 1;
}

int main(void) {
    SetConsoleTitleW(L"Screen Pixelator setup");
    printf("Screen Pixelator setup\n");

    wchar_t self[MAX_PATH * 2];
    DWORD len = GetModuleFileNameW(NULL, self, MAX_PATH * 2);
    if (len == 0 || len >= MAX_PATH * 2) return fail("cannot find this setup file");

    HANDLE f = CreateFileW(self, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) return fail("cannot open this setup file");
    LARGE_INTEGER size, pos;
    if (!GetFileSizeEx(f, &size) || size.QuadPart < 16) { CloseHandle(f); return fail("setup file is damaged"); }

    char tail[16];
    pos.QuadPart = size.QuadPart - 16;
    if (!SetFilePointerEx(f, pos, NULL, FILE_BEGIN) || !read_exact(f, tail, 16) || memcmp(tail, MAGIC, 8) != 0) {
        CloseHandle(f);
        return fail("setup file is damaged (no program files inside)");
    }
    uint64_t offset;
    memcpy(&offset, tail + 8, 8);
    int64_t end = size.QuadPart - 16;
    if ((int64_t)offset >= end) { CloseHandle(f); return fail("setup file is damaged"); }

    wchar_t tmp[MAX_PATH];
    if (!GetTempPathW(MAX_PATH, tmp)) { CloseHandle(f); return fail("no temporary folder"); }
    _snwprintf(g_dir, MAX_PATH * 2, L"%lsScreenPixelatorSetup-%lu", tmp, GetCurrentProcessId());
    CreateDirectoryW(g_dir, NULL);

    printf("Unpacking...\n");
    pos.QuadPart = (LONGLONG)offset;
    SetFilePointerEx(f, pos, NULL, FILE_BEGIN);
    static char buf[1 << 20];
    int64_t at = (int64_t)offset;
    while (at < end) {
        uint32_t nlen;
        uint64_t fsize;
        char name[256];
        wchar_t wname[256];
        if (!read_exact(f, &nlen, 4) || nlen == 0 || nlen >= sizeof(name) || !read_exact(f, name, nlen)) {
            CloseHandle(f);
            return fail("setup file is damaged (name)");
        }
        name[nlen] = 0;
        if (strchr(name, '\\') || strchr(name, '/') || strstr(name, "..") || g_count >= MAX_FILES) {
            CloseHandle(f);
            return fail("setup file is damaged (bad name)");
        }
        if (!read_exact(f, &fsize, 8)) { CloseHandle(f); return fail("setup file is damaged (size)"); }
        MultiByteToWideChar(CP_UTF8, 0, name, -1, wname, 256);
        _snwprintf(g_files[g_count], MAX_PATH * 2, L"%ls\\%ls", g_dir, wname);
        HANDLE out = CreateFileW(g_files[g_count], GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (out == INVALID_HANDLE_VALUE) { CloseHandle(f); return fail("cannot write to the temporary folder"); }
        g_count++;
        uint64_t left = fsize;
        while (left) {
            DWORD chunk = left > sizeof(buf) ? (DWORD)sizeof(buf) : (DWORD)left;
            DWORD written = 0;
            if (!read_exact(f, buf, chunk) || !WriteFile(out, buf, chunk, &written, NULL) || written != chunk) {
                CloseHandle(out);
                CloseHandle(f);
                return fail("could not unpack the program files");
            }
            left -= chunk;
        }
        CloseHandle(out);
        at += 4 + nlen + 8 + (int64_t)fsize;
    }
    CloseHandle(f);

    /* Run the installer script in this console window */
    wchar_t sys[MAX_PATH], cmd[MAX_PATH * 4];
    GetSystemDirectoryW(sys, MAX_PATH);
    _snwprintf(cmd, MAX_PATH * 4,
               L"\"%ls\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoProfile -ExecutionPolicy Bypass -File \"%ls\\install.ps1\"",
               sys, g_dir);
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof si);
    si.cb = sizeof si;
    memset(&pi, 0, sizeof pi);
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, 0, NULL, g_dir, &si, &pi)) {
        return fail("could not start Windows PowerShell");
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    cleanup();
    return finish((int)code);
}
