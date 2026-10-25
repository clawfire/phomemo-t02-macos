/*
 * T02 Bridge launcher — why does this tiny C file exist?
 *
 * macOS kills any process that touches CoreBluetooth without an
 * NSBluetoothAlwaysUsageDescription in *its own* Info.plist. A bare Python
 * interpreter started by launchd has none, so it would crash on the first
 * BLE scan. This launcher lives in a real .app bundle (with that key), and
 * spawns Python as a *child*: children inherit the parent's TCC
 * "responsibility", so the Bluetooth permission is asked for — and granted
 * to — "Phomemo T02 Bridge", once.
 *
 * Usage (from the LaunchAgent):  T02Bridge <program> [args…]
 * Build:  clang -O2 -o T02Bridge launcher.c \
 *           -sectcreate __TEXT __info_plist Info.plist
 */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <program> [args...]\n", argv[0]);
        return 64;
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    pid_t pid;
    int rc = posix_spawn(&pid, argv[1], NULL, NULL, &argv[1], environ);
    if (rc != 0) {
        fprintf(stderr, "T02Bridge: cannot start %s: %s\n", argv[1], strerror(rc));
        return 127;
    }
    child = pid;

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
