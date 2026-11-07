module dd2_g5__nand3_1 (A, B, C, Y);
  input A;
  input B;
  input C;
  output Y;
  assign Y = ~(A & B & C);
endmodule
