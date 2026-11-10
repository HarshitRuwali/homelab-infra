// Progress-marked variant to locate the hang on 395.
#include <cpuid.h>
#include <immintrin.h>
#include <stdio.h>
#include <stdint.h>
#include <signal.h>
#include <setjmp.h>
#include <string.h>
static sigjmp_buf jb; static int which_sig = -1;
static void h(int s){ fprintf(stderr,"  [handler: signal %d]\n",s); which_sig=s; siglongjmp(jb,1); }
static __attribute__((target("avx2"))) float do_gather(const int *idx4) {
    __m128i ix = _mm_loadu_si128((const __m128i *)idx4);
    __m128 r = _mm_i32gather_ps((const float *)0, ix, 1);
    float out[4]; _mm_storeu_ps(out, r);
    return out[0]+out[1]+out[2]+out[3];
}
int main(void){
    setvbuf(stderr, NULL, _IONBF, 0);
    uint32_t eax=0,ebx=0,ecx=0,edx=0; __cpuid(1,eax,ebx,ecx,edx);
    fprintf(stderr,"step1: cpuid.avx=%d\n",(ecx>>28)&1);
    struct sigaction sa; memset(&sa,0,sizeof sa); sa.sa_handler=h; sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV,&sa,NULL); sigaction(SIGBUS,&sa,NULL); sigaction(SIGILL,&sa,NULL);
    fprintf(stderr,"step2: handlers installed\n");
    static float data[4] = {1.f,2.f,3.f,4.f};
    int idxA[4]; for(int i=0;i<4;i++) idxA[i]=(int)(uintptr_t)&data[i];
    fprintf(stderr,"step3: calling do_gather(valid addrs)\n");
    if (sigsetjmp(jb,1)==0) {
        float s = do_gather(idxA);
        fprintf(stderr,"step4: returned sum=%f\n",s);
    } else {
        fprintf(stderr,"step4-ALT: signal %d in probe A\n",which_sig);
    }
    int idxB[4] = {0x1000,0x2000,0x3000,0x4000};
    volatile float sink;
    fprintf(stderr,"step5: calling do_gather(unmapped addrs)\n");
    if (sigsetjmp(jb,1)==0) {
        sink = do_gather(idxB);
        fprintf(stderr,"step6: no fault (unexpected)\n");
    } else {
        fprintf(stderr,"step6-ALT: signal %d in probe B\n",which_sig);
    }
    fprintf(stderr,"DONE\n");
    return 0;
}
