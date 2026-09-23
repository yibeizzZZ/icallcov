#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/* Distinct, known destinations and one repeated indirect callsite. */
__attribute__((noinline)) void target_a(void) { __asm__ volatile("" ::: "memory"); }
__attribute__((noinline)) void target_b(void) { __asm__ volatile("" ::: "memory"); }

__attribute__((noinline)) void dispatch(void (*target)(void)) { target(); }
__attribute__((noinline)) void before_fork(void (*target)(void)) { target(); }
__attribute__((noinline)) void never_called(void (*target)(void)) { target(); }

static void *worker(void *unused)
{
    for (int i = 0; i < 100; ++i) {
        dispatch(target_a);
        dispatch(target_b);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "fork") == 0) {
        before_fork(target_a);
        pid_t child = fork();
        if (child < 0)
            return 1;
        if (child == 0) {
            printf("child %d\n", (int)getpid());
            dispatch(target_b);
            return 0;
        }
        printf("parent %d\n", (int)getpid());
        dispatch(target_a);
        int status;
        if (waitpid(child, &status, 0) != child)
            return 2;
        return WIFEXITED(status) ? WEXITSTATUS(status) : 3;
    }

    pthread_t threads[4];
    for (int i = 0; i < 4; ++i)
        if (pthread_create(&threads[i], NULL, worker, NULL) != 0)
            return 4;
    for (int i = 0; i < 4; ++i)
        if (pthread_join(threads[i], NULL) != 0)
            return 5;
    return 0;
}
