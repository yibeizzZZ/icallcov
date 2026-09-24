/* Build with -O0 -g -emit-llvm to inspect the two indirect calls. */
typedef int (*callback)(int);

int apply_twice(callback fn, int value)
{
    return fn(value) + fn(value + 1);
}

static int increment(int value)
{
    return value + 1;
}

int main(void)
{
    return apply_twice(increment, 0) == 3 ? 0 : 1;
}
