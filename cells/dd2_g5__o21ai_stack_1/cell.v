module dd2_g5__o21ai_stack_1 (A, B, C, Y);
  input A;
  input B;
  input C;
  output Y;
  assign Y = ~((A | B) & C);
endmodule
