/* Event-driven UART/datastore bridge for the sensor firmware.
 * stdin/stdout belong exclusively to the AVR UART; diagnostics use stderr.
 * Local clients use bounded, big-endian length-prefixed binary frames.
 */
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define CLIENTS 8
#define KEYS 32
#define KEY_MAX 64
#define VALUE_MAX 512
#define CLIENT_BUFFER 32768
#define UART_PAYLOAD 512
#define UART_BUFFER 4096
struct entry { unsigned char key[KEY_MAX + 1], value[VALUE_MAX + 1]; size_t kn, vn; };
struct client { int fd; size_t in_used, out_used; unsigned char in[CLIENT_BUFFER], out[CLIENT_BUFFER]; };
static struct entry entries[KEYS];
static struct client clients[CLIENTS];
static size_t entry_count, uart_out_used;
static unsigned char uart_out[UART_BUFFER], packet[UART_PAYLOAD + 6], last_response[VALUE_MAX];
static size_t packet_used, packet_expected, last_length;
static unsigned expected_index = 999;
static uint64_t packet_deadline;
static int listener = -1, quitting;
static volatile sig_atomic_t stopped;
static struct termios saved_termios;
static int termios_saved, saved_in_flags = -1, saved_out_flags = -1;

static uint64_t now_ms(void) {
  struct timespec t;
  if (clock_gettime(CLOCK_MONOTONIC, &t)) { perror("clock_gettime"); exit(1); }
  return (uint64_t)t.tv_sec * 1000 + (uint64_t)t.tv_nsec / 1000000;
}
static uint32_t get32(const unsigned char *p) { return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3]; }
static void put32(unsigned char *p, uint32_t n) { p[0]=n>>24; p[1]=n>>16; p[2]=n>>8; p[3]=n; }
static uint16_t crc_byte(uint16_t crc, unsigned char c) {
  c ^= crc & 255; c ^= (unsigned char)(c << 4);
  return (uint16_t)(((uint16_t)c << 8 | crc >> 8) ^ (c >> 4) ^ ((uint16_t)c << 3));
}
static uint16_t crc(const unsigned char *p, size_t n) {
  uint16_t result=0xffff;
  while (n--) result=crc_byte(result,*p++);
  return result;
}
static int valid_key(const unsigned char *p, size_t n) {
  if (!n || n>KEY_MAX) return 0;
  for (size_t i=0;i<n;i++) if (!((p[i]>='a'&&p[i]<='z') || (p[i]>='A'&&p[i]<='Z') ||
      (p[i]>='0'&&p[i]<='9') || p[i]=='_')) return 0;
  return 1;
}
static struct entry *find(const unsigned char *key, size_t n) {
  for (size_t i=0;i<entry_count;i++) if (entries[i].kn==n && !memcmp(entries[i].key,key,n)) return entries+i;
  return NULL;
}
static struct entry *store(const unsigned char *key, size_t kn, const unsigned char *value, size_t vn) {
  if (!valid_key(key,kn) || vn>VALUE_MAX || memchr(value,0,vn)) return NULL;
  struct entry *e=find(key,kn);
  if (!e) {
    if (entry_count==KEYS) return NULL;
    e=entries+entry_count++; memcpy(e->key,key,kn); e->key[kn]=0; e->kn=kn;
  }
  memcpy(e->value,value,vn); e->value[vn]=0; e->vn=vn;
  return e;
}
static void close_client(struct client *c) {
  if (c->fd>=0) close(c->fd);
  c->fd=-1; c->in_used=c->out_used=0;
}
static int queue_client(struct client *c, unsigned char op, const unsigned char *p, size_t n) {
  if (n+5>CLIENT_BUFFER-c->out_used) { close_client(c); return -1; }
  unsigned char *out=c->out+c->out_used;
  put32(out,(uint32_t)n+1); out[4]=op;
  if (n) memcpy(out+5,p,n);
  c->out_used+=n+5; return 0;
}
static size_t encode_entry(unsigned char *out, const struct entry *e) {
  out[0]=(unsigned char)e->kn; out[1]=e->vn>>8; out[2]=e->vn;
  memcpy(out+3,e->key,e->kn); memcpy(out+3+e->kn,e->value,e->vn);
  return 3+e->kn+e->vn;
}
static void broadcast(unsigned char op, const struct entry *e) {
  unsigned char data[3+KEY_MAX+VALUE_MAX]; size_t n=encode_entry(data,e);
  for (size_t i=0;i<CLIENTS;i++) if (clients[i].fd>=0) queue_client(clients+i,op,data,n);
}
static int client_message(struct client *c, const unsigned char *p, size_t n) {
  if (n==1 && p[0]=='s') {
    unsigned char snapshot[KEYS*(3+KEY_MAX+VALUE_MAX)]; size_t used=0;
    for (size_t i=0;i<entry_count;i++) used+=encode_entry(snapshot+used,entries+i);
    return queue_client(c,'S',snapshot,used);
  }
  if (n<4 || p[0]!='p') return -1;
  size_t kn=p[1], vn=(size_t)p[2]<<8 | p[3];
  if (n!=4+kn+vn) return -1;
  /* Capabilities describe this process, never a writable client snapshot. */
  if (kn==19 && !memcmp(p+4,"resac_native_bridge",19)) return -1;
  struct entry *e=store(p+4,kn,p+4+kn,vn);
  if (!e) return -1;
  broadcast('P',e); return 0;
}
static int ack(unsigned index, const unsigned char *p, size_t n) {
  if (n>VALUE_MAX || n+6>UART_BUFFER-uart_out_used) return -1;
  unsigned char *out=uart_out+uart_out_used;
  out[0]=255; out[1]=(unsigned char)index; out[2]=n>>8; out[3]=n;
  if (n) memcpy(out+4,p,n);
  uint16_t sum=crc(out,n+4); out[n+4]=sum>>8; out[n+5]=sum;
  uart_out_used+=n+6; return 0;
}
static int uart_command(unsigned index, const unsigned char *p, size_t n) {
  if (n==5 && !memcmp(p,"XX",2)) expected_index=index;
  if (expected_index!=index) return expected_index!=999 && index==((expected_index-1)&255) ? ack(index,last_response,last_length) : 0;
  const unsigned char *response=NULL; size_t response_n=0; unsigned char result=0;
  if (n==5 && !memcmp(p,"XX100",5)) { response=&result; response_n=1; }
  else if (n==5 && !memcmp(p,"XXXXX",5)) quitting=1;
  else if (n>1 && p[0]=='d') {
    struct entry *e=find(p+1,n-1);
    if (e) { response=e->value; response_n=e->vn; }
  } else if (n>1 && (p[0]=='D' || p[0]=='B')) {
    const unsigned char *separator=memchr(p+1,254,n-1);
    if (separator) {
      size_t kn=(size_t)(separator-p-1), vn=n-kn-2;
      struct entry *e=store(p+1,kn,separator+1,vn);
      if (e) { result=1; if (p[0]=='B') broadcast('V',e); }
    }
    response=&result; response_n=1;
  } else if (n>4 && p[0]=='M' && !memcmp(p+1,"v1,",3)) {
    const unsigned char *separator=memchr(p+4,',',n-4);
    if (separator) {
      size_t kn=(size_t)(separator-p-4), vn=n-kn-5;
      if (valid_key(p+4,kn) && vn<=VALUE_MAX && !memchr(separator+1,0,vn)) {
        struct entry e; e.kn=kn; e.vn=vn; memcpy(e.key,p+4,kn); memcpy(e.value,separator+1,vn);
        broadcast('V',&e);
      }
    }
  } else { /* Unsupported commands fail closed; no process, file, or IR API. */
    result=0; response=&result; response_n=1;
  }
  if (response_n) memcpy(last_response,response,response_n);
  last_length=response_n;
  if (ack(index,last_response,last_length)) return -1;
  expected_index=(index+1)&255; return 0;
}
static int uart_byte(unsigned char c, uint64_t stamp) {
  if (!packet_used && c!=255) return 0;
  packet[packet_used++]=c; packet_deadline=stamp+50;
  if (packet_used==4) {
    size_t n=(size_t)packet[2]<<8 | packet[3];
    if (n>UART_PAYLOAD) { packet_used=packet_expected=0; return 0; }
    packet_expected=n+6;
  }
  if (packet_expected && packet_used==packet_expected) {
    size_t n=packet_expected-6;
    int good=crc(packet,n+4)==((uint16_t)packet[n+4]<<8 | packet[n+5]);
    packet_used=packet_expected=0;
    if (good) return uart_command(packet[1],packet+4,n);
  }
  return 0;
}
static int nonblocking(int fd) {
  int old=fcntl(fd,F_GETFL);
  return old<0 || fcntl(fd,F_SETFL,old|O_NONBLOCK)<0 ? -1 : old;
}
static void on_signal(int sig) { (void)sig; stopped=1; }
static void restore(void) {
  if (termios_saved) tcsetattr(0,TCSANOW,&saved_termios);
  if (saved_in_flags>=0) fcntl(0,F_SETFL,saved_in_flags);
  if (saved_out_flags>=0) fcntl(1,F_SETFL,saved_out_flags);
  for (size_t i=0;i<CLIENTS;i++) close_client(clients+i);
  if (listener>=0) close(listener);
}
int main(int argc, char **argv) {
  unsigned long port=5701;
  for (size_t i=0;i<CLIENTS;i++) clients[i].fd=-1;
  if (argc==2 && !strcmp(argv[1],"--version")) { puts("res-ac-bridge protocol=1 uart=yun1"); return 0; }
  if (argc==2 && !strcmp(argv[1],"--self-test")) {
    unsigned char encoded[4]; put32(encoded,0x12345678);
    if (get32(encoded)!=0x12345678 || crc((const unsigned char*)"123456789",9)!=0x6f91) return 1;
    (void)now_ms(); puts("PASS: native Bridge clock, framing, and CRC"); return 0;
  }
  if (argc==3 && !strcmp(argv[1],"--port")) {
    char *end; errno=0; port=strtoul(argv[2],&end,10);
    if (errno || !*argv[2] || *end || port>65535) return 2;
  } else if (argc!=1) return 2;
  listener=socket(AF_INET,SOCK_STREAM,0);
  if (listener<0) { perror("socket"); return 1; }
  int yes=1; setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
  struct sockaddr_in addr; memset(&addr,0,sizeof(addr)); addr.sin_family=AF_INET;
  addr.sin_addr.s_addr=htonl(INADDR_LOOPBACK); addr.sin_port=htons((uint16_t)port);
  if (bind(listener,(struct sockaddr*)&addr,sizeof(addr)) || listen(listener,CLIENTS) || nonblocking(listener)<0) {
    perror("listen"); close(listener); return 1;
  }
  socklen_t addr_n=sizeof(addr);
  if (getsockname(listener,(struct sockaddr*)&addr,&addr_n)) { close(listener); return 1; }
  /* Only the local test harness asks for an ephemeral port. */
  if (!tcgetattr(0,&saved_termios)) {
    struct termios raw=saved_termios; cfmakeraw(&raw);
    if (tcsetattr(0,TCSANOW,&raw)) { perror("raw UART"); close(listener); return 1; }
    termios_saved=1;
  }
  saved_in_flags=fcntl(0,F_GETFL); saved_out_flags=fcntl(1,F_GETFL);
  if (saved_in_flags<0 || saved_out_flags<0 || fcntl(0,F_SETFL,saved_in_flags|O_NONBLOCK)<0 || fcntl(1,F_SETFL,saved_out_flags|O_NONBLOCK)<0) { restore(); return 1; }
  atexit(restore); signal(SIGPIPE,SIG_IGN); signal(SIGINT,on_signal); signal(SIGTERM,on_signal);
  if (!store((const unsigned char*)"resac_native_bridge",19,(const unsigned char*)"v1",2)) return 1;
  if (!port) { fprintf(stderr,"READY %u\n",ntohs(addr.sin_port)); fflush(stderr); }
  int status=0;
  while (!stopped && (!quitting || uart_out_used)) {
    fd_set reads,writes; FD_ZERO(&reads); FD_ZERO(&writes); int maxfd=listener;
    if (!quitting) { FD_SET(0,&reads); FD_SET(listener,&reads); }
    if (uart_out_used) FD_SET(1,&writes);
    if (maxfd<1) maxfd=1;
    for (size_t i=0;i<CLIENTS;i++) if (clients[i].fd>=0) {
      FD_SET(clients[i].fd,&reads);
      if (clients[i].out_used) FD_SET(clients[i].fd,&writes);
      if (clients[i].fd>maxfd) maxfd=clients[i].fd;
    }
    struct timeval timeout, *wait=NULL; uint64_t now=now_ms();
    int buffered=0;
    for (size_t i=0;i<CLIENTS;i++) if (clients[i].fd>=0 && clients[i].in_used>=4 &&
        (get32(clients[i].in)>CLIENT_BUFFER-4 || !get32(clients[i].in) || get32(clients[i].in)+4ULL<=clients[i].in_used)) buffered=1;
    if (packet_used) {
      if (now>=packet_deadline) { packet_used=packet_expected=0; }
      else { uint64_t remaining=packet_deadline-now; timeout.tv_sec=remaining/1000; timeout.tv_usec=(remaining%1000)*1000; wait=&timeout; }
    }
    if (buffered) { timeout.tv_sec=timeout.tv_usec=0; wait=&timeout; }
    int ready=select(maxfd+1,&reads,&writes,NULL,wait);
    if (ready<0) { if (errno==EINTR) continue; status=1; break; }
    /* Drain ACKs and UART work before accepting lower-priority socket work. */
    if (FD_ISSET(1,&writes) && uart_out_used) {
      ssize_t n=write(1,uart_out,uart_out_used);
      if (n>0) { memmove(uart_out,uart_out+n,uart_out_used-(size_t)n); uart_out_used-=(size_t)n; }
      else if (n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) { status=1; break; }
    }
    if (FD_ISSET(0,&reads) && !quitting) {
      unsigned char buffer[1024]; ssize_t n=read(0,buffer,sizeof(buffer));
      if (n==0) break;
      if (n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) { status=1; break; }
      uint64_t stamp=now_ms();
      for (ssize_t i=0;i<n && !quitting;i++) if (uart_byte(buffer[i],stamp)) { status=1; stopped=1; break; }
    }
    if (quitting) continue;
    if (FD_ISSET(listener,&reads)) {
      int fd=accept(listener,NULL,NULL);
      if (fd>=0) {
        if (fd>=FD_SETSIZE || nonblocking(fd)<0) close(fd);
        else {
          setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&yes,sizeof(yes));
          size_t i=0; while (i<CLIENTS && clients[i].fd>=0) i++;
          if (i==CLIENTS) close(fd); else clients[i].fd=fd;
        }
      }
    }
    for (size_t i=0;i<CLIENTS;i++) {
      struct client *c=clients+i;
      if (c->fd<0) continue;
      if (FD_ISSET(c->fd,&writes) && c->out_used) {
        ssize_t n=send(c->fd,c->out,c->out_used,0);
        if (n>0) { memmove(c->out,c->out+n,c->out_used-(size_t)n); c->out_used-=(size_t)n; }
        else if (n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) { close_client(c); continue; }
      }
      if (FD_ISSET(c->fd,&reads)) {
        ssize_t n=recv(c->fd,c->in+c->in_used,CLIENT_BUFFER-c->in_used,0);
        if (n<=0) { if (!n || (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR)) close_client(c); continue; }
        c->in_used+=(size_t)n;
      }
      /* Bounded socket work, including frames already read from the kernel.
       * Re-enter select between batches so UART readiness remains first. */
      for (unsigned work=0;work<16 && c->fd>=0 && c->in_used>=4;work++) {
          size_t length=get32(c->in);
          if (!length || length>CLIENT_BUFFER-4) { close_client(c); break; }
          if (c->in_used<length+4) break;
          if (client_message(c,c->in+4,length)) { close_client(c); break; }
          if (c->fd<0) break;
          c->in_used-=length+4; memmove(c->in,c->in+length+4,c->in_used);
      }
    }
  }
  return status;
}
