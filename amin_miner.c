/* amin_miner.c — Agent Mining (AMNT) proof-of-work solver.
 *
 * Proof (verified against the contract's own proofDigest()):
 *   keccak256(abi.encode(sessionSeedBase, wallet, agentId, lane, day, nonce)) <= cpuTarget
 * abi.encode pads every static argument to 32 bytes => 192-byte payload:
 *   [0..31] seed  [32..63] wallet  [64..95] agentId  [96..127] lane  [128..159] day  [160..191] nonce
 *
 * Because the payload is fixed except the last 32 bytes, block 1 (bytes 0..135) is
 * absorbed ONCE and only block 2 (24 fixed bytes + 32 nonce bytes + padding) varies.
 * One keccak permutation per attempt; nonce is written big-endian into the last 8 bytes
 * (valid uint256 values, small integers).
 *
 * Build:
 *   gcc -O3 -funroll-loops -march=native -pthread -o amin_miner amin_miner.c
 * Usage:
 *   ./amin_miner <payload192hex> <bits> <threads> <start> <end>
 * Output (stdout, one JSON per line):
 *   {"nonce":141601296,"hash":"0x..."}
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>
#include <stdatomic.h>
#include <time.h>

#define RATE 136

static const uint64_t RC[24] = {
    0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808aULL,
    0x8000000080008000ULL, 0x000000000000808bULL, 0x0000000080000001ULL,
    0x8000000080008081ULL, 0x8000000000008009ULL, 0x000000000000008aULL,
    0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000aULL,
    0x000000008000808bULL, 0x800000000000008bULL, 0x8000000000008089ULL,
    0x8000000000008003ULL, 0x8000000000008002ULL, 0x8000000000000080ULL,
    0x000000000000800aULL, 0x800000008000000aULL, 0x8000000080008081ULL,
    0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL};

#define ROTL64(x, y) (((x) << (y)) | ((x) >> (64 - (y))))

static const int RC_ROTC[24] = {1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14,
                                27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44};
static const int RC_PILN[24] = {10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4,
                                15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1};

static inline void keccakf(uint64_t st[25]) {
    uint64_t bc[5], t;
    int i, j, r;
    for (r = 0; r < 24; r++) {
        for (i = 0; i < 5; i++)
            bc[i] = st[i] ^ st[i + 5] ^ st[i + 10] ^ st[i + 15] ^ st[i + 20];
        for (i = 0; i < 5; i++) {
            t = bc[(i + 4) % 5] ^ ROTL64(bc[(i + 1) % 5], 1);
            for (j = 0; j < 25; j += 5) st[j + i] ^= t;
        }
        t = st[1];
        for (i = 0; i < 24; i++) {
            j = RC_PILN[i];
            bc[0] = st[j];
            st[j] = ROTL64(t, RC_ROTC[i]);
            t = bc[0];
        }
        for (j = 0; j < 25; j += 5) {
            for (i = 0; i < 5; i++) bc[i] = st[j + i];
            for (i = 0; i < 5; i++) st[j + i] ^= (~bc[(i + 1) % 5]) & bc[(i + 2) % 5];
        }
        st[0] ^= RC[r];
    }
}

static uint64_t g_state1[25];       /* state after absorbing block 1 (fixed) */
static uint8_t g_blk2[136];         /* block 2 template: 24 fixed + 32 nonce + padding */
static int g_bits;
static uint64_t g_start, g_end, g_stride;
static volatile int g_stop;
static _Atomic unsigned long long g_total;

typedef struct { int id; int threads; } arg_t;

static inline int ok_hash(const uint8_t *h) {
    int full = g_bits >> 3, rem = g_bits & 7;
    for (int i = 0; i < full; i++) if (h[i]) return 0;
    if (rem && (h[full] >> (8 - rem))) return 0;
    return 1;
}

static void *worker(void *p) {
    arg_t *a = (arg_t *)p;
    uint64_t st[25];
    uint8_t h[32], blk[136];
    unsigned long long n = g_start + (unsigned long long)a->id;
    const uint64_t step = g_stride ? g_stride : (uint64_t)a->threads;

    memcpy(blk, g_blk2, 136);
    while (!g_stop && n < g_end) {
        /* nonce -> big-endian in the last 8 bytes of the 32-byte nonce field
         * (blk2 payload occupies bytes 0..55; nonce field bytes 24..55) */
        uint64_t v = n;
        for (int i = 55; i >= 48; i--) { blk[i] = (uint8_t)(v & 0xff); v >>= 8; }

        memcpy(st, g_state1, sizeof st);
        for (int i = 0; i < 17; i++) {           /* 136/8 = 17 words */
            uint64_t w;
            memcpy(&w, blk + i * 8, 8);
            st[i] ^= w;
        }
        keccakf(st);
        memcpy(h, st, 32);
        if (ok_hash(h)) {
            printf("{\"nonce\":%llu,\"hash\":\"0x", n);
            for (int i = 0; i < 32; i++) printf("%02x", h[i]);
            printf("\"}\n");
            fflush(stdout);
        }
        g_total++;
        n += step;
    }
    return NULL;
}

static int hex2bin(const char *hex, uint8_t *out, int nbytes) {
    for (int i = 0; i < nbytes; i++) {
        unsigned v;
        if (sscanf(hex + i * 2, "%2x", &v) != 1) return -1;
        out[i] = (uint8_t)v;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 6) {
        fprintf(stderr, "usage: %s <payload192hex> <bits> <threads> <start> <end>\n", argv[0]);
        return 2;
    }
    uint8_t payload[192];
    if (strlen(argv[1]) < 320 || hex2bin(argv[1], payload, 160) != 0) {
        fprintf(stderr, "bad payload\n");
        return 2;
    }
    g_bits = atoi(argv[2]);
    int threads = atoi(argv[3]);
    g_start = strtoull(argv[4], NULL, 10);
    g_end = strtoull(argv[5], NULL, 10);
    g_stride = (uint64_t)threads;
    if (threads < 1) threads = 1;
    if (threads > 128) threads = 128;

    /* block 1 = payload[0..135] absorbed once */
    memset(g_state1, 0, sizeof g_state1);
    for (int i = 0; i < 17; i++) {
        uint64_t w; memcpy(&w, payload + i * 8, 8); g_state1[i] ^= w;
    }
    keccakf(g_state1);

    /* block 2 = 24 fixed bytes (tail of the day field) + 32 nonce bytes + keccak padding */
    memset(g_blk2, 0, 136);
    memcpy(g_blk2, payload + 136, 24);
    g_blk2[56] ^= 0x01;
    g_blk2[135] ^= 0x80;

    pthread_t th[128];
    arg_t args[128];
    for (int i = 0; i < threads; i++) {
        args[i].id = i; args[i].threads = threads;
        pthread_create(&th[i], NULL, worker, &args[i]);
    }
    for (int i = 0; i < threads; i++) pthread_join(th[i], NULL);
    fprintf(stderr, "tried=%llu\n", (unsigned long long)g_total);
    return 0;
}
