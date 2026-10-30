CFLAGS ?= -O2 -Wall -Wextra
LDLIBS := -lmnl

exitlag-netd: exitlag-netd.c protocol.h
	$(CC) $(CFLAGS) -o $@ exitlag-netd.c $(LDLIBS)

clean:
	rm -f exitlag-netd

.PHONY: clean
