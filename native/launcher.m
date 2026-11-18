// Native profile app entry point. No changes to Claude's signed bundle.
#import <AppKit/AppKit.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <sys/stat.h>

static int fail(NSString *message) {
    fprintf(stderr, "manyclaude: %s\n", message.UTF8String);
    if (!isatty(STDERR_FILENO)) {
        [NSApplication sharedApplication];
        NSAlert *alert = [NSAlert new];
        alert.messageText = @"ManyClaude";
        alert.informativeText = message;
        [alert runModal];
    }
    return 1;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSDictionary *info = [NSBundle mainBundle].infoDictionary;
        NSString *source = info[@"ManyClaudeSource"];
        NSString *desktop = info[@"ManyClaudeDesktop"];
        NSString *code = info[@"ManyClaudeCode"];
        NSString *registry = [info[@"ManyClaudeHome"] stringByAppendingPathComponent:@"profiles.json"];
        NSData *data = registry ? [NSData dataWithContentsOfFile:registry] : nil;
        NSDictionary *profiles = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        NSDictionary *profile = profiles[info[@"ManyClaudeKey"]];
        if ([profile[@"desktop"] length]) desktop = profile[@"desktop"];
        if ([profile[@"code"] length]) code = profile[@"code"];
        NSString *secure = profile[@"code_session"] ?: code;
        if (!source || !desktop || !code) return fail(@"Missing profile configuration. Run manyclaude refresh.");
        NSBundle *bundle = [NSBundle bundleWithPath:source];
        NSString *runtime = info[@"ManyClaudeRuntime"];
        if (runtime) {
            struct stat sourceStat;
            NSString *sourceBinary = bundle.executablePath;
            if (!sourceBinary || stat(sourceBinary.fileSystemRepresentation, &sourceStat) != 0)
                return fail(@"The original Claude installation is missing. Reinstall it before launching this profile.");
            NSString *mtime = [NSString stringWithFormat:@"%lld", (long long)sourceStat.st_mtimespec.tv_sec * 1000000000LL + sourceStat.st_mtimespec.tv_nsec];
            NSString *version = bundle.infoDictionary[@"CFBundleVersion"] ?: @"";
            if (![mtime isEqualToString:info[@"ManyClaudeSourceMTime"]] || ![version isEqualToString:info[@"ManyClaudeSourceVersion"]]) {
                NSTask *refresh = [NSTask new];
                refresh.executableURL = [NSURL fileURLWithPath:info[@"ManyClaudeTool"]];
                refresh.arguments = @[@"refresh", info[@"ManyClaudeKey"]];
                NSMutableDictionary *environment = [NSProcessInfo.processInfo.environment mutableCopy];
                environment[@"MANYCLAUDE_HOME"] = info[@"ManyClaudeHome"];
                refresh.environment = environment;
                NSPipe *output = [NSPipe pipe];
                refresh.standardOutput = output;
                refresh.standardError = output;
                NSError *error = nil;
                if (![refresh launchAndReturnError:&error]) return fail(@"Cannot sync this profile with Claude. Run manyclaude refresh.");
                NSData *diagnostic = [output.fileHandleForReading readDataToEndOfFile];
                [refresh waitUntilExit];
                if (refresh.terminationStatus != 0)
                    return fail([[NSString alloc] initWithData:diagnostic encoding:NSUTF8StringEncoding] ?: @"Profile update failed. Run manyclaude refresh.");
            }
        }
        NSString *binary = runtime ? [[NSBundle mainBundle].bundlePath stringByAppendingPathComponent:runtime] : bundle.executablePath;
        if (!binary || access(binary.fileSystemRepresentation, X_OK) != 0)
            return fail(@"Claude Desktop is missing. Reinstall Claude at its original location, then try again.");
        // Dock/Finder launches also pick up skills installed since the last run.
        NSTask *prepare = [NSTask new];
        prepare.executableURL = [NSURL fileURLWithPath:info[@"ManyClaudeTool"]];
        prepare.arguments = @[@"_prepare-skills", info[@"ManyClaudeKey"]];
        NSMutableDictionary *environment = [NSProcessInfo.processInfo.environment mutableCopy];
        environment[@"MANYCLAUDE_HOME"] = info[@"ManyClaudeHome"];
        prepare.environment = environment;
        NSPipe *output = [NSPipe pipe];
        prepare.standardOutput = output;
        prepare.standardError = output;
        NSError *error = nil;
        if (![prepare launchAndReturnError:&error])
            return fail(@"Cannot prepare this profile's skills. Reinstall ManyClaude, then run manyclaude refresh.");
        NSData *diagnostic = [output.fileHandleForReading readDataToEndOfFile];
        [prepare waitUntilExit];
        if (prepare.terminationStatus != 0)
            return fail([[NSString alloc] initWithData:diagnostic encoding:NSUTF8StringEncoding] ?: @"Profile skill preparation failed.");
        // Keep the lock in this supervisor; Electron may close inherited file descriptors.
        NSString *lockPath = [[desktop stringByDeletingLastPathComponent] stringByAppendingPathComponent:@"desktop.lock"];
        int lock = open(lockPath.fileSystemRepresentation, O_CREAT | O_RDWR, 0600);
        if (lock < 0) return fail(@"Cannot open the profile lock file.");
        if (flock(lock, LOCK_EX | LOCK_NB) != 0) {
            char buf[32] = {0};
            pread(lock, buf, sizeof(buf) - 1, 0);
            pid_t pid = (pid_t)strtol(buf, NULL, 10);
            NSRunningApplication *app = pid > 0 ? [NSRunningApplication runningApplicationWithProcessIdentifier:pid] : nil;
            if (app && ([app.bundleURL.path isEqualToString:[NSBundle mainBundle].bundlePath] || [app.bundleURL.path isEqualToString:source]))
                [app activateWithOptions:NSApplicationActivateAllWindows];
            close(lock);
            return 0;
        }
        const char *credentials[] = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_HOST_CREDS_FILE", "ANTHROPIC_PROFILE", NULL};
        for (int i = 0; credentials[i]; i++) unsetenv(credentials[i]);
        setenv("CLAUDE_USER_DATA_DIR", desktop.fileSystemRepresentation, 1);
        setenv("CLAUDE_CONFIG_DIR", code.fileSystemRepresentation, 1);
        setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", secure.fileSystemRepresentation, 1);
        NSString *argument = [@"--user-data-dir=" stringByAppendingString:desktop];
        pid_t pid = fork();
        if (pid < 0) return fail(@"Cannot start Claude Desktop.");
        if (pid == 0) {
            close(lock);
            // Detach from the CLI's stdin. Logs stay inside the profile.
            int input = open("/dev/null", O_RDONLY);
            if (input >= 0) { dup2(input, STDIN_FILENO); close(input); }
            NSString *logPath = [[desktop stringByDeletingLastPathComponent] stringByAppendingPathComponent:@"launcher.log"];
            int log = open(logPath.fileSystemRepresentation, O_CREAT | O_WRONLY | O_APPEND, 0600);
            if (log >= 0) { dup2(log, STDOUT_FILENO); dup2(log, STDERR_FILENO); close(log); }
            execl(binary.fileSystemRepresentation, binary.fileSystemRepresentation, argument.UTF8String, NULL);
            _exit(127);
        }
        ftruncate(lock, 0);
        dprintf(lock, "%d\n", pid);
        int status = 0;
        while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
        close(lock);
        return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    }
}
