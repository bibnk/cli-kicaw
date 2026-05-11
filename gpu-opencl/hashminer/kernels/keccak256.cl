/* keccak256.cl — OpenCL Keccak-256 proof-of-work search for HASH256 ($HASH).
 *
 * Each work item computes  keccak256( challenge[32] || nonce[32]_big_endian )
 * for  nonce = nonce_base + global_id(0)  and reports it iff
 *   uint256(digest_big_endian) < target            (target == on-chain `currentDifficulty`)
 *
 * The preimage is a fixed 64 bytes < the 136-byte Keccak-256 rate, so this is
 * a single absorb block + one Keccak-f[1600] permutation per attempt. Padding
 * is the *original Keccak* pad10*1 (0x01 ... 0x80) — Ethereum's keccak256,
 * NOT NIST SHA3-256 (which would use 0x06). See reference/SPEC.md.
 *
 * Byte/lane layout (all matched on the host side, see gpu.py / constants.py):
 *   - challenge is passed as 4 little-endian 64-bit lanes ch0..ch3, where
 *       ch_i = uint64_le(challenge[8i : 8i+8])           -> XORed into state lanes A[0..3]
 *   - nonce (uint256) is passed as 4 little-endian limbs nb0..nb3 (nb0 = low 64 bits);
 *     nonce = nonce_base + gid; its big-endian byte string fills state lanes A[4..7]
 *     as little-endian uint64 words, which works out to A[4..7] = bswap64(n3..n0).
 *   - pad byte 0x01 lands in A[8] (-> 0x0000000000000001), 0x80 in A[16] (-> 0x8000000000000000).
 *   - digest = LE-bytes(A[0]) || LE-bytes(A[1]) || LE-bytes(A[2]) || LE-bytes(A[3]);
 *     uint256(digest) (big-endian) == bswap64(A[0])<<192 | bswap64(A[1])<<128 | bswap64(A[2])<<64 | bswap64(A[3]).
 *   - target is passed as 4 big-endian limbs t0..t3 (t0 = most significant 64 bits).
 *
 * Output: out_count is a single-element __global uint the host zeroes before each
 * launch; on a hit a thread does idx = atomic_inc(out_count) and, if idx < max_results,
 * writes its nonce as 4 little-endian limbs to out_nonces[idx*4 + 0..3].
 */

#define ROTL64(x, n) (((x) << (n)) | ((x) >> (64UL - (n))))

inline ulong bswap64(ulong x) {
    x = ((x & 0x00FF00FF00FF00FFUL) << 8)  | ((x & 0xFF00FF00FF00FF00UL) >> 8);
    x = ((x & 0x0000FFFF0000FFFFUL) << 16) | ((x & 0xFFFF0000FFFF0000UL) >> 16);
    return (x << 32) | (x >> 32);
}

__constant ulong RC[24] = {
    0x0000000000000001UL, 0x0000000000008082UL, 0x800000000000808aUL, 0x8000000080008000UL,
    0x000000000000808bUL, 0x0000000080000001UL, 0x8000000080008081UL, 0x8000000000008009UL,
    0x000000000000008aUL, 0x0000000000000088UL, 0x0000000080008009UL, 0x000000008000000aUL,
    0x000000008000808bUL, 0x800000000000008bUL, 0x8000000000008089UL, 0x8000000000008003UL,
    0x8000000000008002UL, 0x8000000000000080UL, 0x000000000000800aUL, 0x800000008000000aUL,
    0x8000000080008081UL, 0x8000000000008080UL, 0x0000000080000001UL, 0x8000000080008008UL
};

__constant uint ROTC[24] = { 1u,3u,6u,10u,15u,21u,28u,36u,45u,55u,2u,14u,
                             27u,41u,56u,8u,25u,43u,62u,18u,39u,61u,20u,44u };
__constant uint PILN[24] = { 10u,7u,11u,17u,18u,3u,5u,16u,8u,21u,24u,4u,
                             15u,23u,19u,13u,12u,2u,20u,14u,22u,9u,6u,1u };

inline void keccak_f1600(ulong st[25]) {
    ulong bc[5], t;
    #pragma unroll 1
    for (int r = 0; r < 24; r++) {
        // Theta
        for (int i = 0; i < 5; i++)
            bc[i] = st[i] ^ st[i + 5] ^ st[i + 10] ^ st[i + 15] ^ st[i + 20];
        for (int i = 0; i < 5; i++) {
            t = bc[(i + 4) % 5] ^ ROTL64(bc[(i + 1) % 5], 1UL);
            for (int j = 0; j < 25; j += 5) st[j + i] ^= t;
        }
        // Rho + Pi
        t = st[1];
        for (int i = 0; i < 24; i++) {
            uint j = PILN[i];
            ulong tmp = st[j];
            st[j] = ROTL64(t, (ulong)ROTC[i]);
            t = tmp;
        }
        // Chi
        for (int j = 0; j < 25; j += 5) {
            for (int i = 0; i < 5; i++) bc[i] = st[j + i];
            for (int i = 0; i < 5; i++) st[j + i] ^= (~bc[(i + 1) % 5]) & bc[(i + 2) % 5];
        }
        // Iota
        st[0] ^= RC[r];
    }
}

__kernel void keccak_pow_search(
    const ulong ch0, const ulong ch1, const ulong ch2, const ulong ch3,   // challenge, 4 LE lanes
    const ulong t0,  const ulong t1,  const ulong t2,  const ulong t3,    // target,  4 BE limbs (t0 = MS 64 bits)
    const ulong nb0, const ulong nb1, const ulong nb2, const ulong nb3,   // nonce base, 4 LE limbs (nb0 = low 64 bits)
    const uint  n_total,                                                   // # nonces this launch
    const uint  max_results,
    __global volatile uint*  out_count,                                    // [1], host-zeroed each launch
    __global ulong*          out_nonces)                                   // [max_results * 4], LE limbs per hit
{
    const uint gid = (uint)get_global_id(0);
    if (gid >= n_total) return;

    // nonce = nonce_base + gid  (256-bit add; gid < 2^32 so carry can ripple past nb1 only in pathological wrap cases)
    ulong n0 = nb0 + (ulong)gid;
    ulong c  = (n0 < nb0) ? 1UL : 0UL;
    ulong n1 = nb1 + c;  c = (n1 < nb1) ? 1UL : 0UL;
    ulong n2 = nb2 + c;  c = (n2 < nb2) ? 1UL : 0UL;
    ulong n3 = nb3 + c;

    // Absorb the single 136-byte padded block into the Keccak state.
    ulong st[25];
    st[0]  = ch0;            st[1]  = ch1;            st[2]  = ch2;            st[3]  = ch3;
    st[4]  = bswap64(n3);    st[5]  = bswap64(n2);    st[6]  = bswap64(n1);    st[7]  = bswap64(n0);
    st[8]  = 0x0000000000000001UL;                                          // pad: 0x01 after the 64-byte message
    st[9]  = 0UL; st[10] = 0UL; st[11] = 0UL; st[12] = 0UL;
    st[13] = 0UL; st[14] = 0UL; st[15] = 0UL;
    st[16] = 0x8000000000000000UL;                                          // pad: 0x80 at byte 135 (last of the rate block)
    st[17] = 0UL; st[18] = 0UL; st[19] = 0UL; st[20] = 0UL;
    st[21] = 0UL; st[22] = 0UL; st[23] = 0UL; st[24] = 0UL;

    keccak_f1600(st);

    // uint256(digest) big-endian limbs:
    ulong d0 = bswap64(st[0]);   // most significant 64 bits
    // Compare d0..d3 lexicographically against t0..t3. Bail as early as possible (the common case).
    if (d0 > t0) return;
    if (d0 == t0) {
        ulong d1 = bswap64(st[1]);
        if (d1 > t1) return;
        if (d1 == t1) {
            ulong d2 = bswap64(st[2]);
            if (d2 > t2) return;
            if (d2 == t2) {
                ulong d3 = bswap64(st[3]);
                if (d3 >= t3) return;        // digest == target is NOT < target
            }
        }
    }

    // Hit: uint256(digest) < target.
    uint idx = atomic_inc(out_count);
    if (idx < max_results) {
        __global ulong* slot = out_nonces + (size_t)idx * 4;
        slot[0] = n0; slot[1] = n1; slot[2] = n2; slot[3] = n3;
    }
}
