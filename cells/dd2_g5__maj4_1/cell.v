module dd2_g5__maj4_1 (A, B, C, D, Y);
  input A;
  input B;
  input C;
  input D;
  output Y;
  assign Y = (C & D) | (A & B & (C | D));
endmodule
