module dd2_g5__mux2_opt_1 (A, B, C, Y);
  input A;
  input B;
  input C;
  output Y;
  assign Y = (A & ~C) | (B & C);
endmodule
