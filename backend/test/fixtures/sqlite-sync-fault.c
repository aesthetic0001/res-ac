/* Test-process-only interposition: arm after SQLite initialization. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
static int armed(void) {
  const char *path=getenv("RESAC_TEST_SYNC_ARM");
  return path && access(path,F_OK)==0;
}
static void record(const char *name) {
  const char *path=getenv("RESAC_TEST_SYNC_LOG");
  if (!path) return;
  int fd=open(path,O_WRONLY|O_CREAT|O_APPEND,0600);
  if (fd>=0) {
    ssize_t written=write(fd,name,strlen(name)); close(fd);
    if (written!=(ssize_t)strlen(name)) _exit(99);
  }
}
int fdatasync(int fd) {
  int (*real_sync)(int)=dlsym(RTLD_NEXT,"fdatasync");
  if (armed()) {
    record("fdatasync\n");
    const char *mode=getenv("RESAC_TEST_SYNC_ERROR");
    errno=mode && !strcmp(mode,"ENOSYS")?ENOSYS:
      mode && !strcmp(mode,"EINVAL")?EINVAL:
      mode && !strcmp(mode,"EOPNOTSUPP")?EOPNOTSUPP:EIO;
    return -1;
  }
  return real_sync(fd);
}
int fsync(int fd) {
  int (*real_sync)(int)=dlsym(RTLD_NEXT,"fsync");
  struct stat info;
  if (armed() && fstat(fd,&info)==0) record(S_ISDIR(info.st_mode)?"fsync directory\n":"fsync regular\n");
  return real_sync(fd);
}
