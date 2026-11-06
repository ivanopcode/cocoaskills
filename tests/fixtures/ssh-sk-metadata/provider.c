/* Metadata-only software FIDO fixture. OpenSSH's public middleware ABI is
 * declared in sk-api.h. Generated public bytes and the trace path belong to
 * the disposable test build. No hardware, PIN, touch or signing is supported. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sk-api.h"

static const unsigned char public_bytes[32] = {
#include "public.inc"
};
static void trace(const char *operation) {
    FILE *file = fopen(TRACE_PATH, "a");
    if (file != NULL) { fprintf(file, "%s\n", operation); fclose(file); }
}
uint32_t sk_api_version(void) { return SSH_SK_VERSION_MAJOR; }
int sk_enroll(uint32_t alg, const uint8_t *challenge, size_t length,
    const char *application, uint8_t flags, const char *pin,
    struct sk_option **options, struct sk_enroll_response **out) {
    struct sk_enroll_response *response;
    (void)challenge; (void)length; (void)application; (void)pin; (void)options;
    trace("enroll");
    if (alg != SSH_SK_ED25519) return SSH_SK_ERR_UNSUPPORTED;
    response = calloc(1, sizeof(*response));
    if (response == NULL) return SSH_SK_ERR_GENERAL;
    response->flags = flags;
    response->public_key_len = response->key_handle_len = 32;
    response->public_key = malloc(32);
    response->key_handle = calloc(32, 1);
    if (response->public_key == NULL || response->key_handle == NULL)
        return SSH_SK_ERR_GENERAL;
    memcpy(response->public_key, public_bytes, 32);
    *out = response;
    return 0;
}
int sk_sign(uint32_t alg, const uint8_t *data, size_t length,
    const char *application, const uint8_t *handle, size_t handle_length,
    uint8_t flags, const char *pin, struct sk_option **options,
    struct sk_sign_response **out) {
    (void)alg; (void)data; (void)length; (void)application; (void)handle;
    (void)handle_length; (void)flags; (void)pin; (void)options; (void)out;
    trace("sign");
    return SSH_SK_ERR_UNSUPPORTED;
}
int sk_load_resident_keys(const char *pin, struct sk_option **options,
    struct sk_resident_key ***keys, size_t *count) {
    (void)pin; (void)options; (void)keys; (void)count;
    trace("resident");
    return SSH_SK_ERR_UNSUPPORTED;
}
