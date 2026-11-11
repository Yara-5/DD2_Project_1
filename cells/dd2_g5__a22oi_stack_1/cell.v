module dd2_g5__a22oi_stack_1 (A, B, C, D, Y);
  input A;
  input B;
  input C;
  input D;
  output Y;
  assign Y = ~((A & B) | (C & D));
endmodule
