// e1_test.c (E1): can this guest execute VEX-encoded gather instructions?
//
// The gather is emitted by GCC via an intrinsic in a single target("avx2")
// function; the rest of the binary is plain x86-64 (no AVX), so any #NM/#UD
// observed comes from the gather itself.
//
//   Probe A: vgatherdps with VALID data addresses -> clean result proves the
//            instruction executes at all in this guest.
//   Probe B: same gather, UNMAPPED data addresses -> SIGSEGV means it executed
//            and then faulted on data; SIGILL means CPU/kernel refused to run
//            it (AVX state not enabled / blocked).

#include <cpuid.h>
#include <immintrin.h>
#include <stdio.h>
#include <stdint.h>
#include <signal.h>
#include <setjmp.h>
#include <string.h>

static sigjmp_buf jb;
static int which_sig = -1;

static void h(int s) { which_sig = s; siglongjmp(jb, 1); }

// The only AVX code in the binary. base=0: addresses come from the index reg.
static __attribute__((target("avx2"))) float do_gather(const int *idx4) {
    __m128i ix = _mm_loadu_si128((const __m128i *)idx4);
    __m128 r = _mm_i32gather_ps((const float *)0, ix, 1);
    float out[4];
    _mm_storeu_ps(out, r);
    return out[0] + out[1] + out[2] + out[3];
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    uint32_t eax = 0, ebx = 0, ecx = 0, edx = 0;
    __cpuid(1, eax, ebx, ecx, edx);
    int avx_cpuid = (ecx >> 28) & 1;

    // Read XCR0 up front (before any signal context is in play).
    uint32_t xcr0 = 0;
    __asm__ volatile ("xgetbv" : "=a"(xcr0) : "c"(0));

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = h;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS,  &sa, NULL);
    sigaction(SIGILL,  &sa, NULL);
    sigaction(SIGFPE,  &sa, NULL);

    // ---- Probe A: valid data -------------------------------------------
    static float data[4] = {1.0f, 2.0f, 3.0f, 4.0f};
    int idxA[4];
    for (int i = 0; i < 4; i++) idxA[i] = (int)(uintptr_t)&data[i];

    const char *resA;
    if (sigsetjmp(jb, 1) == 0) {
        float sum = do_gather(idxA);
        resA = (sum == 10.0f) ? "EXECUTED-OK (sum=10)" : "EXECUTED-BAD-DATA";
    } else {
        resA = which_sig == SIGILL ? "SIGILL" : "FAULT";
    }

    // ---- Probe B: unmapped data addresses ------------------------------
    int idxB[4] = {0x1000, 0x2000, 0x3000, 0x4000};

    const char *resB;
    volatile float sinkB;
    if (sigsetjmp(jb, 1) == 0) {
        sinkB = do_gather(idxB);   // volatile: prevents dead-code elimination
        resB = "NO-FAULT (unexpected)";
    } else {
        switch (which_sig) {
        case SIGSEGV: resB = "SIGSEGV (executed, faulted on data)"; break;
        case SIGBUS:  resB = "SIGBUS";  break;
        case SIGILL:  resB = "SIGILL (blocked)"; break;
        default:      resB = "other-signal"; break;
        }
    }

    printf("cpuid.avx=%d\n", avx_cpuid);
    printf("probeA(valid-addr)   : %s\n", resA);
    printf("probeB(unmapped-addr): %s\n", resB);
    printf("xcr0=0x%x (bit4 OSXMMEXTSTATE128=%d)\n", xcr0, (xcr0 >> 4) & 1);
    return 0;
}
