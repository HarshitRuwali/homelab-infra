// E1c: XCR0 manipulation test, handlers installed FIRST.
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
static const char *sn(void){ switch(which_sig){case SIGILL:return "SIGILL";case SIGSEGV:return "SIGSEGV";default:return "other";} }
int main(void){
    setvbuf(stdout,NULL,_IONBF,0);
    struct sigaction sa; memset(&sa,0,sizeof sa); sa.sa_handler=h; sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV,&sa,NULL); sigaction(SIGBUS,&sa,NULL); sigaction(SIGILL,&sa,NULL);

    uint32_t eax=0,ebx=0,ecx=0,edx=0; __cpuid(1,eax,ebx,ecx,edx);
    printf("cpuid: avx=%d xsave(bit26)=%d\n",(ecx>>28)&1,(ecx>>26)&1);

    uint32_t lo=0,hi=0;
    const char *r1;
    if (sigsetjmp(jb,1)==0){ __asm__ volatile("xgetbv":"=a"(lo),"=d"(hi):"c"(0)); r1="OK"; }
    else r1 = sn();
    printf("xgetbv: %s", r1);
    if (!strcmp(r1,"OK")) { printf(" xcr0=0x%x\n", lo);
        const char *r2;
        if (sigsetjmp(jb,1)==0){ uint32_t nlo=lo|0x30, nhi=hi; __asm__ volatile("xsetbv" : "=a"(nlo), "=d"(nhi) : "0"(nlo), "1"(hi), "c"(0)); r2="no-fault"; }
        else r2 = sn();
        uint32_t lo2=0,hi2=0; __asm__ volatile("xgetbv":"=a"(lo2),"=d"(hi2):"c"(0));
        printf("xsetbv(+AVX bits): %s -> xcr0 now 0x%x (bits45=%d)\n", r2, lo2, (lo2>>4)&3);
    } else printf("\n");

    static float data[4] = {1.f,2.f,3.f,4.f};
    int idxA[4]; for(int i=0;i<4;i++) idxA[i]=(int)(uintptr_t)&data[i];
    const char *r3;
    if (sigsetjmp(jb,1)==0){ float s=do_gather(idxA); r3=(s==10.0f)?"EXECUTED-OK":"executed-bad"; }
    else r3 = sn();
    printf("gather: %s\n", r3);
    return 0;
}
