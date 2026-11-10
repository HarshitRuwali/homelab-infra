// E2 victim: keep the gather buffer cache hot with known-pattern data.
#include <immintrin.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <stdint.h>
static float buf[1024] __attribute__((aligned(64)));
static volatile float sink;
static __attribute__((target("avx2"))) void churn_once(void) {
    for (int i = 0; i < 1024; i += 4) {
        int idx[4];
        for (int j = 0; j < 4; j++) idx[j] = (int)(uintptr_t)&buf[i+j];
        __m128 r = _mm_i32gather_ps((const float *)0, _mm_loadu_si128((const __m128i*)idx), 1);
        float out[4]; _mm_storeu_ps(out, r);
        sink = out[0] + out[1];   // keep it alive
    }
}
int main(int argc, char **argv) {
    int secs = atoi(argv[1] ? argv[1] : "6");
    for (int i = 0; i < 1024; i++) buf[i] = (float)i * 0.5f;
    setvbuf(stdout, NULL, _IONBF, 0);
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    long iters = 0;
    do {
        for (int k = 0; k < 2000; k++) churn_once();
        iters += 2000;
        clock_gettime(CLOCK_MONOTONIC, &t1);
    } while ((long)(t1.tv_sec - t0.tv_sec) < secs);
    printf("victim: done iters=%ld sink=%.3f\n", iters, (double)sink);
    return 0;
}
