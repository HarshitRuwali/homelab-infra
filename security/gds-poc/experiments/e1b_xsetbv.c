// E1b: can a tenant guest re-enable AVX state via xsetbv despite hidden CPUID?
#include <cpuid.h>
#include <immintrin.h>
#include <stdio.h>
#include <stdint.h>
#include <signal.h>
#include <setjmp.h>
#include <string.h>
static sigjmp_buf jb; static int which_sig = -1;
static void h(int s){ which_sig=s; siglongjmp(jb,1); }
static __attribute__((target("avx2"))) float do_gather(const int *idx4) {
    __m128i ix = _mm_loadu_si128((const __m128i *)idx4);
    __m128 r = _mm_i32gather_ps((const float *)0, ix, 1);
    float out[4]; _mm_storeu_ps(out, r);
    return out[0]+out[1]+out[2]+out[3];
}
static void xgetbv(uint32_t *lo, uint32_t *hi){ __asm__ volatile("xgetbv":"=a"(*lo),"=d"(*hi):"c"(0)); }
int main(void){
    setvbuf(stdout,NULL,_IONBF,0);
    uint32_t eax=0,ebx=0,ecx=0,edx=0; __cpuid(1,eax,ebx,ecx,edx);
    printf("cpuid.avx=%d\n",(ecx>>28)&1);
    struct sigaction sa; memset(&sa,0,sizeof sa); sa.sa_handler=h; sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV,&sa,NULL); sigaction(SIGBUS,&sa,NULL); sigaction(SIGILL,&sa,NULL);

    uint32_t lo=0, hi=0; xgetbv(&lo,&hi);
    printf("xcr0 before: 0x%x\n", lo);

    // Try to enable AVX state (bits 4+5) via xsetbv.
    const char *xset_result;
    if (sigsetjmp(jb,1)==0) {
        __asm__ volatile("xsetbv" : : "c"(0), "a"((uint32_t)(lo|0x30)), "d"(hi));
        xgetbv(&lo,&hi);
        xset_result = (lo & 0x30) ? "SUCCEEDED (bits stuck)" : "no-op (bits not set)";
    } else {
        xset_result = which_sig==SIGILL ? "#UD (blocked)" : "fault";
    }
    printf("xsetbv(+AVX bits): %s\n", xset_result);

    // Now try the gather.
    static float data[4] = {1.f,2.f,3.f,4.f};
    int idxA[4]; for(int i=0;i<4;i++) idxA[i]=(int)(uintptr_t)&data[i];
    const char *gather;
    if (sigsetjmp(jb,1)==0) {
        float s = do_gather(idxA);
        gather = (s==10.0f) ? "EXECUTED-OK (sum=10)" : "executed-bad-data";
    } else {
        gather = which_sig==SIGILL ? "SIGILL" : "fault";
    }
    printf("gather after xsetbv: %s\n", gather);

    uint32_t lo2=0,hi2=0; xgetbv(&lo2,&hi2);
    printf("xcr0 after: 0x%x\n", lo2);
    return 0;
}
