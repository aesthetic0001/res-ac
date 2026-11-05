/* Persistent HTTPS transport. Private length-prefixed IPC on stdin/stdout;
 * one libcurl multi handle owns the connection pool for the helper lifetime.
 * Build: scripts/build_http_transport.py. See device/README.md for the protocol.
 */
#define _POSIX_C_SOURCE 200809L
#include <curl/curl.h>
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SLOTS 4
#define BODY_LIMIT 32768U
#define FRAME_LIMIT 37000U
#define QUEUE_LIMIT (SLOTS * (BODY_LIMIT + 256U))

struct request {
  CURL *easy;
  struct curl_slist *headers;
  uint32_t id;
  unsigned char *payload;
  unsigned char body[BODY_LIMIT];
  size_t used;
  char error[CURL_ERROR_SIZE];
};
static struct request requests[SLOTS];
static CURLM *multi;
static CURLSH *shared;
static char *base_url, *ca_file, *authorization;
static unsigned char input[FRAME_LIMIT + 4], output[QUEUE_LIMIT];
static size_t input_used, output_used;

static uint32_t u32(const unsigned char *p) {
  uint32_t n; memcpy(&n, p, 4); return ntohl(n);
}
static void put32(unsigned char *p, uint32_t n) {
  n = htonl(n); memcpy(p, &n, 4);
}
static void release(struct request *r) {
  if (r->easy) { curl_multi_remove_handle(multi, r->easy); curl_easy_cleanup(r->easy); }
  curl_slist_free_all(r->headers);
  free(r->payload);
  memset(r, 0, sizeof(*r));
}
static char *field(const unsigned char **p, size_t *remaining, size_t limit) {
  if (*remaining < 4) return NULL;
  uint32_t n = u32(*p); *p += 4; *remaining -= 4;
  if (n > limit || n > *remaining || memchr(*p, 0, n)) return NULL;
  char *s = malloc(n + 1);
  if (!s) return NULL;
  memcpy(s, *p, n); s[n] = 0; *p += n; *remaining -= n;
  return s;
}
static size_t receive_body(char *data, size_t size, size_t count, void *context) {
  struct request *r = context;
  if (size && count > BODY_LIMIT / size) return 0;
  size_t n = size * count;
  if (n > BODY_LIMIT - r->used) return 0;
  memcpy(r->body + r->used, data, n); r->used += n;
  return n;
}
static uint32_t reason(const char *error) {
  if (strstr(error, "Connection refused") || strstr(error, "connection refused")) return 1;
  if (strstr(error, "Network is unreachable") || strstr(error, "network is unreachable")) return 2;
  if (strstr(error, "No route to host") || strstr(error, "no route to host")) return 3;
  if (strstr(error, "Connection reset") || strstr(error, "connection reset")) return 4;
  if (strstr(error, "Broken pipe") || strstr(error, "broken pipe")) return 5;
  return 0;
}
static int complete(struct request *r, CURLcode code) {
  /* Response: id, curl code, HTTP status, new connections, safe error category,
   * five cumulative times in microseconds, body length, body. No raw errors. */
  const size_t metadata = 44, length = metadata + r->used;
  if (output_used + length + 4 > sizeof(output)) return -1;
  unsigned char *p = output + output_used;
  put32(p, (uint32_t)length); p += 4;
  long status = 0, connections = 0;
  curl_easy_getinfo(r->easy, CURLINFO_RESPONSE_CODE, &status);
  curl_easy_getinfo(r->easy, CURLINFO_NUM_CONNECTS, &connections);
  put32(p, r->id); put32(p + 4, code); put32(p + 8, (uint32_t)status);
  put32(p + 12, (uint32_t)connections); put32(p + 16, reason(r->error));
  const CURLINFO phases[] = {CURLINFO_NAMELOOKUP_TIME, CURLINFO_CONNECT_TIME,
    CURLINFO_APPCONNECT_TIME, CURLINFO_STARTTRANSFER_TIME, CURLINFO_TOTAL_TIME};
  for (size_t i = 0; i < 5; ++i) {
    double seconds = 0;
    curl_easy_getinfo(r->easy, phases[i], &seconds);
    put32(p + 20 + i * 4, (uint32_t)(seconds * 1000000.0));
  }
  put32(p + 40, (uint32_t)r->used);
  memcpy(p + metadata, r->body, r->used);
  output_used += length + 4;
  release(r);
  return 0;
}
static int configure(const unsigned char *p, size_t n) {
  if (base_url) return -1;
  base_url = field(&p, &n, 2048);
  ca_file = field(&p, &n, 1024);
  char *token = field(&p, &n, 256);
  if (!base_url || !ca_file || !token || n || strncmp(base_url, "https://", 8) ||
      strchr(token, '\r') || strchr(token, '\n')) { free(token); return -1; }
  authorization = malloc(strlen(token) + 23);
  if (!authorization) { free(token); return -1; }
  sprintf(authorization, "Authorization: Bearer %s", token);
  memset(token, 0, strlen(token)); free(token);
  return 0;
}
static int submit(const unsigned char *p, size_t n) {
  if (!base_url || n < 9) return -1;
  uint32_t id = u32(p), timeout = u32(p + 4);
  unsigned char method = p[8]; p += 9; n -= 9;
  if (!id || timeout < 1 || timeout > 60 || (method != 'G' && method != 'P')) return -1;
  struct request *r = NULL;
  for (size_t i = 0; i < SLOTS; ++i) {
    if (requests[i].easy && requests[i].id == id) return -1;
    if (!requests[i].easy) r = &requests[i];
  }
  if (!r) return -1;
  char *route = field(&p, &n, 128), *parent = field(&p, &n, 55);
  unsigned char *payload = (unsigned char *)field(&p, &n, BODY_LIMIT);
  if (!route || !parent || !payload || n || strlen(parent) != 55 ||
      strchr(parent, '\r') || strchr(parent, '\n') || route[0] == '/' || strstr(route, "..")) {
    free(route); free(parent); free(payload); return -1;
  }
  char url[2200], trace_header[70];
  if (snprintf(url, sizeof(url), "%s%s", base_url, route) >= (int)sizeof(url)) {
    free(route); free(parent); free(payload); return -1;
  }
  snprintf(trace_header, sizeof(trace_header), "traceparent: %s", parent);
  free(route); free(parent);
  r->easy = curl_easy_init(); r->id = id; r->payload = payload;
  if (!r->easy) { release(r); return -1; }
  const char *headers[] = {authorization, trace_header, "Content-Type: application/json", "Expect:"};
  for (size_t i = 0; i < sizeof(headers) / sizeof(headers[0]); ++i) {
    struct curl_slist *next = curl_slist_append(r->headers, headers[i]);
    if (!next) { release(r); return -1; }
    r->headers = next;
  }
#define OPTION(name, value) do { if (curl_easy_setopt(r->easy, name, value) != CURLE_OK) { release(r); return -1; } } while (0)
  OPTION(CURLOPT_URL, url);
  OPTION(CURLOPT_CAINFO, ca_file);
  OPTION(CURLOPT_HTTPHEADER, r->headers);
  OPTION(CURLOPT_SSL_VERIFYPEER, 1L);
  OPTION(CURLOPT_SSL_VERIFYHOST, 2L);
  OPTION(CURLOPT_PROTOCOLS_STR, "https");
  OPTION(CURLOPT_PROXY, "");
  OPTION(CURLOPT_FOLLOWLOCATION, 0L);
  OPTION(CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_1_1);
  OPTION(CURLOPT_CONNECTTIMEOUT, 10L);
  OPTION(CURLOPT_TIMEOUT, (long)timeout);
  OPTION(CURLOPT_NOSIGNAL, 1L);
  OPTION(CURLOPT_WRITEFUNCTION, receive_body);
  OPTION(CURLOPT_WRITEDATA, r);
  OPTION(CURLOPT_ERRORBUFFER, r->error);
  OPTION(CURLOPT_PRIVATE, r);
  OPTION(CURLOPT_SHARE, shared);
  if (method == 'P') {
    OPTION(CURLOPT_POST, 1L);
    OPTION(CURLOPT_POSTFIELDS, r->payload);
    OPTION(CURLOPT_POSTFIELDSIZE, (long)strlen((const char *)r->payload));
  }
#undef OPTION
  if (curl_multi_add_handle(multi, r->easy) != CURLM_OK) { release(r); return -1; }
  return 0;
}
static int command(const unsigned char *p, size_t n) {
  if (!n) return -1;
  unsigned char kind = *p++; --n;
  if (kind == 'I') return configure(p, n);
  if (kind == 'Q') return submit(p, n);
  if (kind == 'C' && n == 4) {
    for (size_t i = 0; i < SLOTS; ++i)
      if (requests[i].easy && requests[i].id == u32(p)) release(&requests[i]);
    return 0;
  }
  return -1;
}
static int receive_commands(void) {
  ssize_t got = read(STDIN_FILENO, input + input_used, sizeof(input) - input_used);
  if (!got) return -1;
  if (got < 0) return (errno == EAGAIN || errno == EINTR) ? 0 : -1;
  input_used += (size_t)got;
  while (input_used >= 4) {
    size_t n = u32(input);
    if (!n || n > FRAME_LIMIT) return -1;
    if (input_used < n + 4) break;
    if (command(input + 4, n)) return -1;
    input_used -= n + 4;
    memmove(input, input + n + 4, input_used);
  }
  return 0;
}
int main(int argc, char **argv) {
  if (argc == 2 && !strcmp(argv[1], "--version")) {
    printf("res-ac-http protocol=1 libcurl=%s\n", curl_version_info(CURLVERSION_NOW)->version);
    return 0;
  }
  if (argc != 1) return 2;
  signal(SIGPIPE, SIG_IGN);
  if (fcntl(STDIN_FILENO, F_SETFL, O_NONBLOCK) || fcntl(STDOUT_FILENO, F_SETFL, O_NONBLOCK) ||
      curl_global_init(CURL_GLOBAL_DEFAULT)) return 2;
  multi = curl_multi_init(); shared = curl_share_init();
  if (!multi || !shared) return 2;
  curl_share_setopt(shared, CURLSHOPT_SHARE, CURL_LOCK_DATA_SSL_SESSION);
  curl_multi_setopt(multi, CURLMOPT_MAX_TOTAL_CONNECTIONS, (long)SLOTS);
  curl_multi_setopt(multi, CURLMOPT_MAX_HOST_CONNECTIONS, (long)SLOTS);
  curl_multi_setopt(multi, CURLMOPT_MAXCONNECTS, (long)SLOTS);
  int result = 0;
  for (;;) {
    int running;
    if (curl_multi_perform(multi, &running) != CURLM_OK) { result = 2; break; }
    int remaining;
    CURLMsg *message;
    while ((message = curl_multi_info_read(multi, &remaining))) {
      if (message->msg == CURLMSG_DONE) {
        struct request *r;
        curl_easy_getinfo(message->easy_handle, CURLINFO_PRIVATE, &r);
        if (complete(r, message->data.result)) { result = 2; goto done; }
      }
    }
    /* Wake on incoming work, network progress, outgoing capacity or libcurl's
     * nearest internal deadline. Completion is delivered as an IPC message. */
    struct curl_waitfd extra[] = {{STDIN_FILENO, CURL_WAIT_POLLIN, 0},
      {STDOUT_FILENO, output_used ? CURL_WAIT_POLLOUT : 0, 0}};
    if (curl_multi_poll(multi, extra, 2, 60000, NULL) != CURLM_OK) { result = 2; break; }
    if ((extra[0].revents & CURL_WAIT_POLLIN) && receive_commands()) break;
    if (extra[1].revents & CURL_WAIT_POLLOUT) {
      ssize_t sent = write(STDOUT_FILENO, output, output_used);
      if (sent < 0 && errno != EAGAIN && errno != EINTR) { result = 2; break; }
      if (sent > 0) {
        output_used -= (size_t)sent;
        memmove(output, output + sent, output_used);
      }
    }
  }
done:
  for (size_t i = 0; i < SLOTS; ++i) release(&requests[i]);
  curl_multi_cleanup(multi); curl_share_cleanup(shared); curl_global_cleanup();
  free(base_url); free(ca_file); free(authorization);
  return result;
}
