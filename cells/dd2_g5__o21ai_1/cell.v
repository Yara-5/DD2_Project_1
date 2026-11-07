module dd2_g5__o21ai_1 (A1, A2, B1, Y);
  input A1;
  input A2;
  input B1;
  output Y;
  assign Y = ~((A1 | A2) & B1);
endmodule
