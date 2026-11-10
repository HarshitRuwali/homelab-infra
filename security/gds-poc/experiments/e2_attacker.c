// E2 attacker: sample single-gather latency into a histogram.
#include <immintrin.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <stdint.h>
static float buf[4] __attribute__((aligned(64)));
static int idx[4];
static float acc = 0;
#define BUCKETS 128          // 4-cycle buckets -> covers to 512 cycles, then overflow
static unsigned long hist[BUCKETS + 1], nsamp = 0;
static __attribute__((target("avx2"))) void sample(void) {
    unsigned lo, hi;
    __asm__ volatile ("lfence");
    __asm__ volatile ("rdtsc" : "=a"(lo), "=d"(hi));
    unsigned long t0 = ((unsigned long)hi << 32) | lo;
    __m128 r = _mm_i32gather_ps((const float *)0, _mm_loadu_si128((const __m128i*)idx), 1);
    float out[4]; _mm_storeu_ps(out, r);
    acc += out[0] + out[1];
    __asm__ volatile ("lfence");
    __asm__ volatile ("rdtsc" : "=a"(lo), "=d"(hi));
    unsigned long t1 = ((unsigned long)hi << 32) | lo;
    unsigned long d = t1 - t0;
    hist[d < (BUCKETS * 4UL) ? d / 4 : BUCKETS]++;
    nsamp++;
}
int main(int argc, char **argv) {
    int secs = atoi(argv[1] ? argv[1] : "6");
    for (int i = 0; i < 4; i++) buf[i] = 1.0f + i;
    for (int i = 0; i < 4; i++) idx[i] = (int)(uintptr_t)&buf[i];
    setvbuf(stdout, NULL, _IONBF, 0);
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    do {
        for (long k = 0; k < 200000; k++) sample();
        clock_gettime(CLOCK_MONOTONIC, &t1);
    } while ((long)(t1.tv_sec - t0.tv_sec) < secs);
    unsigned long sum = 0;
    for (int b = 0; b <= BUCKETS; b++) {
        if (!hist[b]) continue;
        printf("bucket[%4d-%4d cyc]: %lu\n", b*4, b*4+3, hist[b]);
        sum += hist[b] * (unsigned long)(b < BUCKETS ? b*2 : 999); // rough mean weight
    }
    printf("attacker: nsamp=%lu mean~%lu acc=%.3f\n", nsamp, nsamp?sum/nsamp:0, (double)acc);
    return 0;
}
