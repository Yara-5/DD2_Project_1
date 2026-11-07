module dd2_g5__nand2_1 (A, B, Y);
  input A;
  input B;
  output Y;
  assign Y = (~A & ~B) | (A & ~B) | (~A & B);
endmodule
