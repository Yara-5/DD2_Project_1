// Broken case for model_level_check.py: NAND2 with the A=1, B=1 row wrong (Y=1).
module dd2_g5__nand2_1 (A, B, Y);
  input A;
  input B;
  output Y;
  assign Y = ~(A & B) | (A & B);
endmodule
