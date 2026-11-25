// An answer that belongs to what was typed when it was asked for: a check of one key is not a check of the key typed after it. A form keeps its
// last answer with the signature of its inputs and shows it only while the inputs still have that signature.
export interface Signed<T> {
  signature: string;
  value: T;
}

/** The held answer if it was made with this signature; null otherwise. */
export const signedFor = <T>(held: Signed<T> | null, signature: string): T | null => (held?.signature === signature ? held.value : null);
